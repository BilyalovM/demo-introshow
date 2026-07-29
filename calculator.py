from typing import List, Dict, Any, Optional


def merge_identical_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Склеивает одинаковые позиции (имя + цена + дни + тип) в одну строку с суммарным qty.

    Нужно, когда в каталоге несколько единиц одной модели лежат отдельными SKU —
    в смете и PDF показываем «Сабвуфер — 2 шт», а не две строки по 1 шт.
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for item in items:
        key = (
            (item.get("name") or "").strip().lower(),
            float(item.get("price") or 0),
            int(item.get("days") or 1),
            item.get("category_type", "equipment"),
            item.get("warehouse_type") or "own",
        )
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["quantity"] = int(item.get("quantity") or 1)
            order.append(key)
        else:
            merged[key]["quantity"] = int(merged[key].get("quantity") or 0) + int(item.get("quantity") or 1)
            # Сохраняем доп. поля, если у первой строки их не было
            for field in ("photo_url", "description", "equipment_id", "category",
                          "warehouse_type", "cost_price", "supplier"):
                if not merged[key].get(field) and item.get(field):
                    merged[key][field] = item[field]
    return [merged[k] for k in order]


DEFAULT_TAX_PERCENTAGE = 16.0


def calculate_estimate(
    items: List[Dict[str, Any]],
    discount_percentage: float,
    tax_percentage: Optional[float] = DEFAULT_TAX_PERCENTAGE,
) -> Dict[str, Any]:
    """
    Calculates the total estimate based on selected items, their quantities,
    duration, discount and tax (fixed 16% by business rule).

    The discount ONLY applies to items where 'category_type' == 'equipment'.
    Fixed items ('category_type' == 'fixed') are not discounted.

    Each item in `items` should have:
    - name (str)
    - price (float)
    - quantity (int)
    - days (int)
    - category_type (str): 'equipment' or 'fixed'
    Optional passthrough: warehouse_type, cost_price, equipment_id, category, supplier
    """
    items = merge_identical_items(items)

    equipment_base = 0.0
    equipment_after = 0.0
    own_equipment_base = 0.0
    own_equipment_after = 0.0
    subrental_base = 0.0
    subrental_after = 0.0
    fixed_sum = 0.0
    cost_total = 0.0

    if discount_percentage is None or discount_percentage < 0 or discount_percentage > 100:
        discount_percentage = 0.0
    # Налог всегда 16% (игнорируем входящее значение, кроме явного None → тоже 16)
    tax_percentage = DEFAULT_TAX_PERCENTAGE

    discount_multiplier = 1.0 - (discount_percentage / 100.0)

    processed_items = []

    for item in items:
        qty = int(item.get("quantity") or 1)
        days = int(item.get("days") or 1)
        price = float(item.get("price") or 0)
        line_total_base = price * qty * days
        cat_type = item.get("category_type", "equipment")
        warehouse_type = item.get("warehouse_type") or "own"
        cost_price = float(item.get("cost_price") or 0)

        if cat_type == "equipment":
            line_total_discounted = line_total_base * discount_multiplier
            equipment_base += line_total_base
            equipment_after += line_total_discounted
            line_discount = line_total_base - line_total_discounted
            if warehouse_type == "subrental":
                subrental_base += line_total_base
                subrental_after += line_total_discounted
            else:
                own_equipment_base += line_total_base
                own_equipment_after += line_total_discounted
        else:
            line_total_discounted = line_total_base
            fixed_sum += line_total_discounted
            line_discount = 0.0

        if warehouse_type == "subrental":
            cost_total += cost_price * qty * days

        processed_items.append({
            "name": item.get("name"),
            "price": price,
            "quantity": qty,
            "days": days,
            "category_type": cat_type,
            "line_total_base": line_total_base,
            "line_total_discounted": line_total_discounted,
            "discount_amount": line_discount,
            "photo_url": item.get("photo_url"),
            "description": item.get("description"),
            "warehouse_type": warehouse_type,
            "cost_price": cost_price,
            "equipment_id": item.get("equipment_id"),
            "category": item.get("category"),
            "supplier": item.get("supplier"),
            "line_cost": cost_price * qty * days if warehouse_type == "subrental" else 0.0,
        })

    discount_amount = equipment_base - equipment_after
    after_discount = equipment_after + fixed_sum
    tax_amount = after_discount * (tax_percentage / 100.0)
    grand_total = after_discount + tax_amount
    margin = grand_total - cost_total

    return {
        "items": processed_items,
        "equipment_total": equipment_after,  # после скидки (совместимость: свой + субаренда)
        "equipment_base": equipment_base,
        "own_equipment_total": own_equipment_after,
        "own_equipment_base": own_equipment_base,
        "subrental_total": subrental_after,
        "subrental_base": subrental_base,
        "fixed_total": fixed_sum,
        "discount_amount": discount_amount,
        "after_discount": after_discount,
        "tax_percentage": tax_percentage,
        "tax_amount": tax_amount,
        "grand_total": grand_total,
        "cost_total": cost_total,
        "margin": margin,
        "discount_percentage": discount_percentage,
    }
