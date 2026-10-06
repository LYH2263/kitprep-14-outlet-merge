from app.services.bom_engine import (
    MergeRejected, ReconciliationError, build_merged_result, explode_and_merge,
    merge_prep, merged_result_to_dict, reconcile_prep_result,
)

import pytest

def test_explode_merge():
    order_lines = [{"dish_id": 1, "portions": 10}, {"dish_id": 2, "portions": 5}]
    bom = [
        {"dish_id": 1, "ingredient_id": 1, "qty_per_portion": 0.2},
        {"dish_id": 1, "ingredient_id": 2, "qty_per_portion": 0.1},
        {"dish_id": 2, "ingredient_id": 1, "qty_per_portion": 0.3},
    ]
    ings = {
        1: {"code": "A", "name": "肉", "unit": "kg", "stock_qty": 1.0},
        2: {"code": "B", "name": "米", "unit": "kg", "stock_qty": 5.0},
    }
    lines = explode_and_merge(order_lines, bom, ings)
    by_id = {l.ingredient_id: l for l in lines}
    assert by_id[1].need_qty == 3.5  # 10*0.2 + 5*0.3
    assert by_id[1].shortage == 2.5
    assert by_id[2].need_qty == 1.0
    assert by_id[2].shortage == 0.0

def test_no_negative_shortage():
    order_lines = [{"dish_id": 1, "portions": 1}]
    bom = [{"dish_id": 1, "ingredient_id": 1, "qty_per_portion": 1.0}]
    ings = {1: {"code": "A", "name": "油", "unit": "L", "stock_qty": 10.0}}
    lines = explode_and_merge(order_lines, bom, ings)
    assert lines[0].shortage == 0.0

# ---------- 两店合单 ----------

BOM = [
    {"dish_id": 1, "ingredient_id": 1, "qty_per_portion": 0.2},
    {"dish_id": 1, "ingredient_id": 2, "qty_per_portion": 0.1},
    {"dish_id": 2, "ingredient_id": 1, "qty_per_portion": 0.3},
]
INGS = {
    1: {"code": "I-MEAT", "name": "肉", "unit": "kg", "stock_qty": 2.0},
    2: {"code": "I-RICE", "name": "米", "unit": "kg", "stock_qty": 5.0},
}


def two_stores():
    return [
        {"order_id": 101, "code": "KO-A", "outlet": "城西门店",
         "lines": [{"dish_id": 1, "portions": 10}, {"dish_id": 2, "portions": 5}]},
        {"order_id": 202, "code": "KO-B", "outlet": "城东门店",
         "lines": [{"dish_id": 2, "portions": 4}]},
    ]


def test_merge_totals_equal_sum_of_stores():
    result = build_merged_result(two_stores(), BOM, INGS)
    by_id = {l["ingredient_id"]: l for l in result["prep_lines"]}
    # 肉：A = 10*0.2 + 5*0.3 = 3.5，B = 4*0.3 = 1.2，合计 4.7；库存 2 → 缺 2.7
    assert by_id[1]["need_qty"] == 4.7
    assert by_id[1]["shortage"] == 2.7
    # 米：只有 A 需要 10*0.1 = 1.0；B 不要米，但行必须在（落 A 的份额，不丢）
    assert by_id[2]["need_qty"] == 1.0
    owners = {s["order_id"] for s in by_id[2]["stores"]}
    assert owners == {101}
    # 肉行两家都必须挂上
    assert {s["order_id"] for s in by_id[1]["stores"]} == {101, 202}
    assert result["stats"]["total_need_qty"] == 5.7
    per = {r["order_id"]: r["need_qty"] for r in result["stats"]["per_store"]}
    assert per == {101: 4.5, 202: 1.2}  # 两店分量之和 == 5.7


def test_merge_shortages_and_stats_derive_from_same_lines():
    result = build_merged_result(two_stores(), BOM, INGS)
    # 缺料贴必须就是备料行中 shortage>0 的同一条（同一对象派生）
    short_ids = [s["ingredient_id"] for s in result["shortages"]]
    assert short_ids == [l["ingredient_id"] for l in result["prep_lines"] if l["shortage"] > 0]
    assert result["stats"]["shortage_count"] == len(result["shortages"]) == 1
    assert result["stats"]["total_shortage_qty"] == 2.7
    for s in result["shortages"]:
        line = next(l for l in result["prep_lines"] if l["ingredient_id"] == s["ingredient_id"])
        for key in ("ingredient_id", "ingredient_code", "need_qty", "stock_qty", "shortage"):
            assert s[key] == line[key]


def test_merge_shortage_never_credited_to_wrong_store_code():
    result = build_merged_result(two_stores(), BOM, INGS)
    meat = next(l for l in result["prep_lines"] if l["ingredient_id"] == 1)
    # 缺料挂在原料编码 I-MEAT 下；门店份额各自独立，B 的 0.8 不许记到 A（101）名下
    assert meat["ingredient_code"] == "I-MEAT"
    share = {s["order_id"]: s["need_qty"] for s in meat["stores"]}
    assert share == {101: 3.5, 202: 1.2}


def test_merge_rejects_empty_or_single_store():
    with pytest.raises(MergeRejected):
        build_merged_result([], BOM, INGS)
    one = two_stores()[:1]
    with pytest.raises(MergeRejected):
        build_merged_result(one, BOM, INGS)


def test_merge_rejects_duplicate_order_and_same_outlet():
    dup = two_stores()
    dup[1] = {**dup[0]}
    with pytest.raises(MergeRejected):
        build_merged_result(dup, BOM, INGS)
    same_outlet = two_stores()
    same_outlet[1] = {**same_outlet[1], "outlet": "城西门店"}
    with pytest.raises(MergeRejected):
        build_merged_result(same_outlet, BOM, INGS)


def test_merge_rejects_empty_lines_and_dish_without_bom():
    no_lines = two_stores()
    no_lines[1]["lines"] = []
    with pytest.raises(MergeRejected):
        build_merged_result(no_lines, BOM, INGS)
    bad_dish = two_stores()
    bad_dish[1]["lines"] = [{"dish_id": 99, "portions": 1}]
    with pytest.raises(MergeRejected):
        build_merged_result(bad_dish, BOM, INGS)


def test_reconcile_catches_dropped_store_lines():
    # 手工构造"行只落一家"的坏结果：肉行缺了 B 的份额
    lines, orders = merge_prep(two_stores(), BOM, INGS)
    result = merged_result_to_dict(lines, orders)
    meat = next(l for l in result["prep_lines"] if l["ingredient_id"] == 1)
    meat["stores"] = [s for s in meat["stores"] if s["order_id"] != 202]
    meat["need_qty"] = 3.5  # 只按 A 算
    with pytest.raises(ReconciliationError):
        reconcile_prep_result(result, two_stores(), BOM, INGS)


def test_reconcile_catches_shortage_count_mismatch():
    # 坏结果：行是两家加总，缺料贴却被偷偷多加一条/数字不符
    lines, orders = merge_prep(two_stores(), BOM, INGS)
    result = merged_result_to_dict(lines, orders)
    result["shortages"] = [dict(result["shortages"][0]), dict(result["shortages"][0])]
    with pytest.raises(ReconciliationError):
        reconcile_prep_result(result, two_stores(), BOM, INGS)


def test_reconcile_catches_cross_store_code_attribution():
    # 坏结果：把 B 的需求量记到 A 的订单名下
    lines, orders = merge_prep(two_stores(), BOM, INGS)
    result = merged_result_to_dict(lines, orders)
    meat = next(l for l in result["prep_lines"] if l["ingredient_id"] == 1)
    for s in meat["stores"]:
        if s["order_id"] == 202:
            s["order_id"] = 101
            s["outlet"] = "城西门店"
            s["code"] = "KO-A"
    with pytest.raises(ReconciliationError):
        reconcile_prep_result(result, two_stores(), BOM, INGS)


def test_reconcile_catches_foreign_order_in_split():
    lines, orders = merge_prep(two_stores(), BOM, INGS)
    result = merged_result_to_dict(lines, orders)
    meat = next(l for l in result["prep_lines"] if l["ingredient_id"] == 1)
    meat["stores"].append({"order_id": 999, "code": "KO-X", "outlet": "第三家", "need_qty": 0.001})
    meat["need_qty"] = round(meat["need_qty"] + 0.001, 3)
    with pytest.raises(ReconciliationError):
        reconcile_prep_result(result, two_stores(), BOM, INGS)
