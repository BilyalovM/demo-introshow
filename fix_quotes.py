import re

with open('templates/quotes.html', 'r') as f:
    html = f.read()

# Remove the old <style> block completely to rely on dashboard.css
html = re.sub(r'<style>.*?</style>', '''<style>
    .item-row {
        display: grid;
        grid-template-columns: 2fr 1.5fr 1fr 1.5fr auto;
        gap: 12px;
        align-items: flex-end;
        margin-bottom: 16px;
        padding-bottom: 16px;
        border-bottom: 1px dashed var(--border);
    }
    .totals {
        margin-top: 24px;
        padding-top: 16px;
        border-top: 1px solid var(--border);
    }
    .total-row {
        display: flex;
        justify-content: space-between;
        margin-bottom: 8px;
        font-size: 14px;
        font-weight: 600;
        color: var(--text-2);
    }
    .grand-total {
        font-weight: 800;
        font-size: 20px;
        color: var(--primary);
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--border-2);
    }
    .autocomplete-list { 
        position: absolute; top: 100%; left: 0; right: 0; 
        background: var(--surface); border: 1px solid var(--border); 
        border-radius: var(--r-sm); max-height: 250px; overflow-y: auto; 
        z-index: 100; display: none; box-shadow: var(--shadow-md);
    }
    .autocomplete-item { padding: 10px 14px; cursor: pointer; border-bottom: 1px solid var(--border-2); font-size: 13.5px; }
    .autocomplete-item:hover { background-color: var(--surface-2); color: var(--primary); }
    
    /* modal classes from dashboard.css if needed, else custom */
    .modal {
        display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%;
        background-color: rgba(17, 24, 39, 0.4); backdrop-filter: blur(4px);
        align-items: center; justify-content: center;
    }
    .modal-content {
        background-color: var(--surface); border-radius: var(--r-lg); width: 90%; max-width: 1000px;
        box-shadow: var(--shadow-lg); position: relative; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden;
    }
</style>''', html, flags=re.DOTALL)

# Replace container with grid cols-2
html = html.replace('<div class="container">', '<div class="grid cols-2">')

# Replace form-groups with label field
def replace_form_group(match):
    label_text = match.group(1)
    input_html = match.group(2)
    return f'<label class="field">\n                    <span>{label_text}</span>\n                    {input_html}\n                </label>'

html = re.sub(r'<div class="form-group">\s*<label>(.*?)</label>\s*(<input[^>]+>|<select[^>]+>|<textarea[^>]+>)\s*</div>', replace_form_group, html)

# For inputs with autocomplete-list inside form-group
def replace_form_group_complex(match):
    inner = match.group(1)
    inner = inner.replace('<label>', '<span>').replace('</label>', '</span>')
    return f'<label class="field" style="position: relative;">\n                    {inner}\n                </label>'

html = re.sub(r'<div class="form-group">([\s\S]*?)</div>', replace_form_group_complex, html)

# Fix h2 and h3 inside cards
html = html.replace('<h2>', '<h3 class="section-title" style="margin-bottom: 16px;">')
html = html.replace('</h2>', '</h3>')
html = html.replace('<h3>', '<h3 class="section-title" style="margin-bottom: 16px; margin-top: 24px;">')

# Fix buttons
html = html.replace('class="remove-btn"', 'class="btn" style="background: var(--danger-soft); color: var(--danger); border-color: transparent;"')

# Fix the itemTemplate content
html = html.replace('''            <div style="position: relative;">
                <label style="font-size: 0.8rem;">Позиция</label>''', '''            <label class="field" style="position: relative; margin: 0;">
                <span>Позиция</span>''')

html = html.replace('''                <div class="autocomplete-list equip-list"></div>
            </div>''', '''                <div class="autocomplete-list equip-list"></div>
            </label>''')

html = html.replace('''            <div>
                <label style="font-size: 0.8rem;">Категория</label>''', '''            <label class="field" style="margin: 0;">
                <span>Категория</span>''')
html = html.replace('''                </select>
            </div>''', '''                </select>
            </label>''')

html = html.replace('''            <div>
                <label style="font-size: 0.8rem;">Цена (₸)</label>''', '''            <label class="field" style="margin: 0;">
                <span>Цена (₸)</span>''')
html = html.replace('''                <input type="number" class="item-price" value="0" min="0" onchange="calculateTotals()" required>
            </div>''', '''                <input type="number" class="item-price num" value="0" min="0" onchange="calculateTotals()" required>
            </label>''')

html = html.replace('''            <div>
                <label style="font-size: 0.8rem;">Кол-во * Дни</label>''', '''            <label class="field" style="margin: 0;">
                <span>Кол-во * Дни</span>''')
html = html.replace('''                </div>
            </div>''', '''                </div>
            </label>''')

with open('templates/quotes.html', 'w') as f:
    f.write(html)

print("Fixed quotes.html")
