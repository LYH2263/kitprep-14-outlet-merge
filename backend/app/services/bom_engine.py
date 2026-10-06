"""Central kitchen BOM explode: order lines × BOM qty, merge ingredients, shortage = need - stock."""
from __future__ import annotations
from dataclasses import asdict, dataclass

@dataclass
class NeedLine:
    ingredient_id: int
    ingredient_code: str
    ingredient_name: str
    unit: str
    need_qty: float
    stock_qty: float
    shortage: float

def explode_and_merge(
    order_lines: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> list[NeedLine]:
    """order_lines: dish_id, portions; bom_lines: dish_id, ingredient_id, qty_per_portion."""
    need: dict[int, float] = {}
    for ol in order_lines:
        for bl in bom_lines:
            if bl["dish_id"] != ol["dish_id"]:
                continue
            need[bl["ingredient_id"]] = need.get(bl["ingredient_id"], 0.0) + ol["portions"] * bl["qty_per_portion"]
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
            need_qty=round(qty, 3),
            stock_qty=round(stock, 3),
            shortage=round(shortage, 3),
        ))
    return lines

def result_to_dict(lines: list[NeedLine]) -> dict:
    return {
        "prep_lines": [asdict(l) for l in lines],
        "shortages": [asdict(l) for l in lines if l.shortage > 0],
        "stats": {
            "ingredient_count": len(lines),
            "shortage_count": sum(1 for l in lines if l.shortage > 0),
            "total_shortage_qty": round(sum(l.shortage for l in lines), 3),
        },
    }

# need_qty/shortage 均按 0.001 取整，加总与合单各自取整最多差 0.001，容差取两倍
MERGE_TOLERANCE = 0.002

def verify_merge_consistency(merged: dict, parts: list[dict]) -> list[str]:
    """合单对账：两店备料行加总必须等于合单备料行，缺料贴/统计必须与备料行一致。

    返回问题列表；为空表示对得上、允许落库。只要对不上，调用方必须整次失败，
    两边门店都不许新落行，更不许把一家的缺料记到另一家编码下去凑平。
    """
    problems: list[str] = []
    for idx, part in enumerate(parts):
        problems.extend(_check_internal_consistency(part, f"门店{idx + 1}"))
    problems.extend(_check_internal_consistency(merged, "合单"))
    summed: dict[int, float] = {}
    for part in parts:
        for line in part.get("prep_lines", []):
            iid = line["ingredient_id"]
            summed[iid] = summed.get(iid, 0.0) + float(line["need_qty"])
    merged_lines = {l["ingredient_id"]: l for l in merged.get("prep_lines", [])}
    for iid, qty in sorted(summed.items()):
        line = merged_lines.get(iid)
        if line is None:
            problems.append(f"合单备料行缺原料 {iid}（两店加总 {round(qty, 3)}）")
        elif abs(float(line["need_qty"]) - round(qty, 3)) > MERGE_TOLERANCE:
            problems.append(f"原料 {iid} 合单需求 {line['need_qty']} ≠ 两店加总 {round(qty, 3)}")
    for iid in sorted(merged_lines):
        if iid not in summed:
            problems.append(f"合单备料行多出两店没有的原料 {iid}")
    return problems

def _check_internal_consistency(result: dict, label: str) -> list[str]:
    """单份结果内部自洽：缺料贴 == 备料行中 shortage>0 的行，统计 == 由缺料贴推出。"""
    problems: list[str] = []
    lines = result.get("prep_lines", [])
    shortages = result.get("shortages", [])
    stats = result.get("stats", {})
    expected = [l for l in lines if float(l.get("shortage", 0)) > 0]
    got = [(s["ingredient_id"], round(float(s["shortage"]), 3)) for s in shortages]
    want = [(e["ingredient_id"], round(float(e["shortage"]), 3)) for e in expected]
    if got != want:
        problems.append(f"{label}缺料贴与备料行对不上")
    if stats.get("ingredient_count") != len(lines):
        problems.append(f"{label}统计 ingredient_count 与备料行数不符")
    if stats.get("shortage_count") != len(shortages):
        problems.append(f"{label}统计 shortage_count 与缺料贴不符")
    total = round(sum(float(s["shortage"]) for s in shortages), 3)
    if abs(float(stats.get("total_shortage_qty", 0.0)) - total) > MERGE_TOLERANCE:
        problems.append(f"{label}统计 total_shortage_qty 与缺料贴不符")
    return problems
