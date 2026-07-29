from typing import List, Dict, Any


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
        )
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["quantity"] = int(item.get("quantity") or 1)
            order.append(key)
        else:
            merged[key]["quantity"] = int(merged[key].get("quantity") or 0) + int(item.get("quantity") or 1)
            # Сохраняем доп. поля, если у первой строки их не было
            for field in ("photo_url", "description", "equipment_id"):
                if not merged[key].get(field) and item.get(field):
                    merged[key][field] = item[field]
    return [merged[k] for k in order]


def calculate_estimate(items: List[Dict[str, Any]], discount_percentage: float) -> Dict[str, Any]:
    """
    Calculates the total estimate based on selected items, their quantities,
    duration, and the applied discount.
    
    The discount ONLY applies to items where 'category_type' == 'equipment'.
    Fixed items ('category_type' == 'fixed') are not discounted.
    
    Each item in `items` should have:
    - name (str)
    - price (float)
    - quantity (int)
    - days (int)
    - category_type (str): 'equipment' or 'fixed'
    """
    items = merge_identical_items(items)

    equipment_sum = 0.0
    fixed_sum = 0.0
    
    # Validation
    if discount_percentage < 0 or discount_percentage > 100:
        discount_percentage = 0.0
        
    discount_multiplier = 1.0 - (discount_percentage / 100.0)
    
    processed_items = []
    
    for item in items:
        # Calculate base price for this line item
        line_total_base = item['price'] * item['quantity'] * item['days']
        cat_type = item.get('category_type', 'equipment')
        
        # Calculate discounted price and final total
        if cat_type == 'equipment':
            line_total_discounted = line_total_base * discount_multiplier
            equipment_sum += line_total_discounted
            discount_amount = line_total_base - line_total_discounted
        else:
            line_total_discounted = line_total_base
            fixed_sum += line_total_discounted
            discount_amount = 0.0
            
        processed_items.append({
            'name': item['name'],
            'price': item['price'],
            'quantity': item['quantity'],
            'days': item['days'],
            'category_type': cat_type,
            'line_total_base': line_total_base,
            'line_total_discounted': line_total_discounted,
            'discount_amount': discount_amount,
            'photo_url': item.get('photo_url'),
            'description': item.get('description'),
        })
        
    total_cost = equipment_sum + fixed_sum
    
    return {
        'items': processed_items,
        'equipment_total': equipment_sum,
        'fixed_total': fixed_sum,
        'grand_total': total_cost,
        'discount_percentage': discount_percentage
    }
