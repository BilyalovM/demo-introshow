from typing import List, Dict, Any

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
            'discount_amount': discount_amount
        })
        
    total_cost = equipment_sum + fixed_sum
    
    return {
        'items': processed_items,
        'equipment_total': equipment_sum,
        'fixed_total': fixed_sum,
        'grand_total': total_cost,
        'discount_percentage': discount_percentage
    }
