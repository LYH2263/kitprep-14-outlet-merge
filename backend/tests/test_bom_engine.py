from app.services.bom_engine import explode_and_merge, result_to_dict, verify_merge_consistency

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

_BOM = [
    {"dish_id": 1, "ingredient_id": 1, "qty_per_portion": 0.2},
    {"dish_id": 1, "ingredient_id": 2, "qty_per_portion": 0.1},
    {"dish_id": 2, "ingredient_id": 1, "qty_per_portion": 0.3},
]
_INGS = {
    1: {"code": "A", "name": "肉", "unit": "kg", "stock_qty": 1.0},
    2: {"code": "B", "name": "米", "unit": "kg", "stock_qty": 50.0},
}

def _result(order_lines):
    return result_to_dict(explode_and_merge(order_lines, _BOM, _INGS))

def _two_store_parts():
    part_a = _result([{"dish_id": 1, "portions": 10}])
    part_b = _result([{"dish_id": 2, "portions": 5}])
    merged = _result([{"dish_id": 1, "portions": 10}, {"dish_id": 2, "portions": 5}])
    return merged, [part_a, part_b]

def test_verify_merge_consistency_ok():
    merged, parts = _two_store_parts()
    assert verify_merge_consistency(merged, parts) == []

def test_verify_merge_consistency_catches_missing_store_rows():
    """只落下一家行、缺料贴却按两店加总：必须判为对不上。"""
    merged, parts = _two_store_parts()
    keep_id = merged["prep_lines"][0]["ingredient_id"]
    merged["prep_lines"] = [l for l in merged["prep_lines"] if l["ingredient_id"] == keep_id]
    assert verify_merge_consistency(merged, parts)

def test_verify_merge_consistency_catches_qty_mismatch():
    merged, parts = _two_store_parts()
    merged["prep_lines"][0]["need_qty"] += 1.0
    assert verify_merge_consistency(merged, parts)

def test_verify_merge_consistency_catches_bad_stats():
    merged, parts = _two_store_parts()
    merged["stats"]["total_shortage_qty"] += 1.0
    assert verify_merge_consistency(merged, parts)

def test_verify_merge_consistency_catches_shortage_misattribution():
    """把一家的缺料记到另一家编码下凑数：必须判为对不上。"""
    merged, parts = _two_store_parts()
    donor = parts[0]["shortages"][0]
    parts[1]["shortages"] = [dict(donor)]
    parts[1]["stats"]["shortage_count"] = 1
    parts[1]["stats"]["total_shortage_qty"] = donor["shortage"]
    assert verify_merge_consistency(merged, parts)
