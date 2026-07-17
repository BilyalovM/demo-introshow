from bs4 import BeautifulSoup
import re

with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

# We need to replace the catalog container and the cart container
soup = BeautifulSoup(html, 'html.parser')

# Find the h3 tags to identify the cards
catalog_card = None
cart_card = None

for card in soup.find_all('div', class_='card'):
    h3 = card.find('h3')
    if h3 and "Каталог" in h3.text:
        catalog_card = card
    elif h3 and "Позиции сметы" in h3.text:
        cart_card = card

if catalog_card:
    # Remove all children after h3
    for el in catalog_card.find_all('div', class_='cat-group'):
        el.decompose()
    # Add a container
    container = soup.new_tag('div', id='catalogContainer')
    catalog_card.append(container)

if cart_card:
    # Remove the muted p
    p = cart_card.find('p', class_='muted')
    if p: p.decompose()
    # Add cart container
    container = soup.new_tag('div', id='cartContainer')
    p_empty = soup.new_tag('p', id='cartEmpty', attrs={'class': 'muted'})
    p_empty.string = "Пока пусто — добавьте позиции из каталога выше."
    cart_card.append(p_empty)
    cart_card.append(container)

# We also need to add IDs to the inputs
# "Аренда с" is in the 3rd card...
date_inputs = soup.find_all('input', type='date')
if len(date_inputs) >= 3:
    date_inputs[1]['id'] = 'rentStart'
    date_inputs[2]['id'] = 'rentEnd'

# Let's write the soup back and add our JS
html = str(soup)

js_code = """
{% block extra_scripts %}
<style>
.conflict-icon {
    color: red;
    font-weight: bold;
    cursor: pointer;
    margin-left: 8px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    background: #fee2e2;
    border-radius: 50%;
    font-size: 12px;
}
.conflict-tooltip {
    display: none;
    position: absolute;
    background: white;
    border: 1px solid red;
    padding: 8px;
    border-radius: 4px;
    z-index: 1000;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    font-size: 12px;
    color: black;
    white-space: pre-wrap;
}
.cat-name { position: relative; }
</style>
<script>
let equipment = [];
let folders = [];
let cart = [];
let conflictsData = {}; // { eq_id: { booked, conflicts: [] } }

async function loadCatalog() {
    const [resEq, resFold] = await Promise.all([
        fetch('/api/equipment'),
        fetch('/api/folders')
    ]);
    equipment = await resEq.json();
    folders = await resFold.json();
    renderCatalog();
}

function renderCatalog() {
    const container = document.getElementById('catalogContainer');
    container.innerHTML = '';
    
    // Group by folder
    const grouped = {};
    equipment.forEach(eq => {
        const fname = eq.folder ? eq.folder.name : (eq.category || 'Без категории');
        if(!grouped[fname]) grouped[fname] = [];
        grouped[fname].push(eq);
    });
    
    for (const [fname, items] of Object.entries(grouped)) {
        const groupDiv = document.createElement('div');
        groupDiv.className = 'cat-group';
        
        const headDiv = document.createElement('div');
        headDiv.className = 'cat-head';
        headDiv.innerHTML = `<span>${fname}</span>`;
        groupDiv.appendChild(headDiv);
        
        const table = document.createElement('table');
        table.className = 'data catalog-table';
        const tbody = document.createElement('tbody');
        
        items.forEach(item => {
            const tr = document.createElement('tr');
            
            // Check conflicts
            let conflictHtml = '';
            let conflictTooltipHtml = '';
            if (conflictsData[item.id]) {
                const c = conflictsData[item.id];
                // if booked >= stock (or if we just want to warn if booked > 0)
                if (c.booked > 0) {
                    const tooltipText = c.conflicts.map(x => `Договор: ${x.deal_title}\\nДаты: ${x.dates}\\nКол-во: ${x.qty}`).join('\\n\\n');
                    conflictHtml = `<span class="conflict-icon" onmouseenter="this.nextElementSibling.style.display='block'" onmouseleave="this.nextElementSibling.style.display='none'">!</span>
                    <div class="conflict-tooltip">Забронировано: ${c.booked} шт.\\nНа складе: ${item.stock_quantity}\\n\\n${tooltipText}</div>`;
                }
            }
            
            tr.innerHTML = `
                <td class="cat-name">${item.name} ${conflictHtml}</td>
                <td class="num muted cat-price">${item.price} ₸</td>
                <td class="num" style="width:60px">
                    <button class="btn btn-sm btn-primary" onclick="addToCart(${item.id})">+</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        groupDiv.appendChild(table);
        container.appendChild(groupDiv);
    }
}

async function checkAvailability() {
    const start = document.getElementById('rentStart').value;
    const end = document.getElementById('rentEnd').value;
    if (!start || !end) return;
    
    const res = await fetch(`/api/equipment/availability?start_date=${start}&end_date=${end}`);
    const data = await res.json();
    conflictsData = {};
    data.forEach(d => { conflictsData[d.equipment_id] = d; });
    renderCatalog();
}

document.getElementById('rentStart').addEventListener('change', checkAvailability);
document.getElementById('rentEnd').addEventListener('change', checkAvailability);

function addToCart(id) {
    const item = equipment.find(e => e.id === id);
    if (!item) return;
    const existing = cart.find(c => c.id === id);
    if (existing) {
        existing.qty += 1;
    } else {
        cart.push({ ...item, qty: 1, days: 1 });
    }
    renderCart();
}

function removeFromCart(id) {
    cart = cart.filter(c => c.id !== id);
    renderCart();
}

function updateCartItem(id, field, val) {
    const item = cart.find(c => c.id === id);
    if(item) {
        item[field] = parseInt(val) || 1;
        renderCart();
    }
}

function renderCart() {
    const container = document.getElementById('cartContainer');
    const empty = document.getElementById('cartEmpty');
    container.innerHTML = '';
    
    if (cart.length === 0) {
        empty.style.display = 'block';
        calculateTotals();
        return;
    }
    empty.style.display = 'none';
    
    const table = document.createElement('table');
    table.className = 'data';
    const tbody = document.createElement('tbody');
    
    cart.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${item.name}</td>
            <td class="num"><input type="number" class="num" style="width:50px; padding:4px;" value="${item.qty}" min="1" onchange="updateCartItem(${item.id}, 'qty', this.value)"> шт</td>
            <td class="num"><input type="number" class="num" style="width:50px; padding:4px;" value="${item.days}" min="1" onchange="updateCartItem(${item.id}, 'days', this.value)"> дн</td>
            <td class="num">${item.price * item.qty * item.days} ₸</td>
            <td style="width:30px"><button class="btn btn-sm" style="color:red;border:none;background:none;" onclick="removeFromCart(${item.id})">×</button></td>
        `;
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
    
    calculateTotals();
}

function calculateTotals() {
    let eqTotal = 0;
    let fixedTotal = 0;
    
    cart.forEach(item => {
        const isFixed = ["Логистика", "Персонал", "Расходники"].includes(item.category);
        const sum = item.price * item.qty * item.days;
        if (isFixed) fixedTotal += sum;
        else eqTotal += sum;
    });
    
    const discountInputs = document.querySelectorAll('.card input.num[type="number"]');
    const discountPercent = discountInputs.length > 0 ? (parseFloat(discountInputs[0].value) || 0) : 0;
    const taxPercent = discountInputs.length > 1 ? (parseFloat(discountInputs[1].value) || 0) : 0;
    
    const discountSum = (eqTotal * discountPercent) / 100;
    const afterDiscount = eqTotal - discountSum + fixedTotal;
    const taxSum = (afterDiscount * taxPercent) / 100;
    const grandTotal = afterDiscount + taxSum;
    
    // Update DOM totals
    const rows = document.querySelectorAll('.totals .row span:last-child');
    if(rows.length >= 7) {
        rows[0].innerText = `${eqTotal} ₸`;
        rows[1].innerText = `${fixedTotal} ₸`;
        rows[2].innerText = `${eqTotal + fixedTotal} ₸`;
        rows[3].innerText = `−${discountSum} ₸`;
        rows[4].innerText = `${afterDiscount} ₸`;
        rows[5].innerText = `${taxSum} ₸`;
        rows[6].innerText = `${grandTotal} ₸`;
    }
}

// Add event listener to recalculate totals when discount/tax inputs change
document.querySelectorAll('.card input.num[type="number"]').forEach(inp => {
    inp.addEventListener('change', calculateTotals);
});

window.onload = loadCatalog;
</script>
{% endblock %}
"""

# Append js block before </body> or at the end
if "{% block extra_scripts %}" not in html:
    html += js_code

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Updated quotes_new.html")
