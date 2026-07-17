import re

with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

# Change to input event
html = html.replace("inp.addEventListener('change', calculateTotals);", "inp.addEventListener('input', calculateTotals);")

# Update calculateTotals
old_calc = """    // Update DOM totals
    const rows = document.querySelectorAll('.totals .row span:last-child');
    if(rows.length >= 7) {
        rows[0].innerText = `${eqTotal} ₸`;
        rows[1].innerText = `${fixedTotal} ₸`;
        rows[2].innerText = `${eqTotal + fixedTotal} ₸`;
        rows[3].innerText = `−${discountSum} ₸`;
        rows[4].innerText = `${afterDiscount} ₸`;
        rows[5].innerText = `${taxSum} ₸`;
        rows[6].innerText = `${grandTotal} ₸`;
    }"""

new_calc = """    // Update DOM totals
    const rows = document.querySelectorAll('.totals .row span:last-child');
    const labelRows = document.querySelectorAll('.totals .row span:first-child');
    if(rows.length >= 7) {
        rows[0].innerText = `${eqTotal.toLocaleString('ru-RU')} ₸`;
        rows[1].innerText = `${fixedTotal.toLocaleString('ru-RU')} ₸`;
        rows[2].innerText = `${(eqTotal + fixedTotal).toLocaleString('ru-RU')} ₸`;
        rows[3].innerText = `−${discountSum.toLocaleString('ru-RU')} ₸`;
        rows[4].innerText = `${afterDiscount.toLocaleString('ru-RU')} ₸`;
        rows[5].innerText = `${taxSum.toLocaleString('ru-RU')} ₸`;
        rows[6].innerText = `${grandTotal.toLocaleString('ru-RU')} ₸`;
        
        if (labelRows.length >= 7) {
            labelRows[3].innerText = `Скидка ${discountPercent}%`;
            labelRows[5].innerText = `Налог ${taxPercent}%`;
        }
    }"""

html = html.replace(old_calc, new_calc)

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
