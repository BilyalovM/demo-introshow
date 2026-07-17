let equipment = [];
let folders = [];
let cart = [];
let conflictsData = {}; // { eq_id: { booked, conflicts: [] } }


async function loadCatalog() {
    try {
        const [resEq, resFold] = await Promise.all([
            fetch('/api/equipment?t=' + new Date().getTime()),
            fetch('/api/folders?t=' + new Date().getTime())
        ]);
        if (!resEq.ok || !resFold.ok) {
            alert('Failed to fetch data: ' + resEq.status + ' ' + resFold.status);
            return;
        }
        equipment = await resEq.json();
        folders = await resFold.json();
        renderCatalog();
    } catch (e) {
        alert('Error loading catalog: ' + e.message);
        console.error(e);
    }
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
                    const tooltipText = c.conflicts.map(x => `Договор: ${x.deal_title}\nДаты: ${x.dates}\nКол-во: ${x.qty}`).join('\n\n');
                    conflictHtml = `<span class="conflict-icon" onmouseenter="this.nextElementSibling.style.display='block'" onmouseleave="this.nextElementSibling.style.display='none'">!</span>
                    <div class="conflict-tooltip">Забронировано: ${c.booked} шт.\nНа складе: ${item.stock_quantity}\n\n${tooltipText}</div>`;
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

loadCatalog();

{% if quote_id %}
    // Load quote data
    async function fetchQuoteData() {
        try {
            const res = await fetch('/api/deals/{{ quote_id }}');
            if (res.ok) {
                const deal = await res.json();
                
                // Populate inputs
                const dateInputs = document.querySelectorAll('input[type="date"]');
                if (dateInputs.length >= 3) {
                    dateInputs[1].value = deal.setup_date || '';
                    dateInputs[2].value = deal.event_date || '';
                }
                
                const textInputs = document.querySelectorAll('.card input');
                // Title
                if (textInputs[1]) textInputs[1].value = deal.title || '';
                // Event address
                if (textInputs[3]) textInputs[3].value = deal.event_address || '';
                
                // Populate cart
                if (deal.items && deal.items.length > 0) {
                    deal.items.forEach(it => {
                        const eq = equipment.find(e => e.id === it.equipment_id);
                        if (eq) {
                            cart.push({ ...eq, qty: it.quantity, days: it.days });
                        }
                    });
                    renderCart();
                }
            }
        } catch(e) { console.error(e); }
    }
    
    // Call it after loadCatalog completes
    const oldRenderCatalog = renderCatalog;
    renderCatalog = function() {
        oldRenderCatalog();
        fetchQuoteData();
    }
{% endif %}



async function saveQuote() {
    if (cart.length === 0) {
        alert('Добавьте позиции в смету');
        return;
    }
    const start = document.getElementById('rentStart').value;
    const end = document.getElementById('rentEnd').value;
    if (!start || !end) {
        alert('Укажите даты аренды (Аренда с и Аренда по)');
        return;
    }
    
    const payload = {
        title: "Смета от " + new Date().toLocaleDateString('ru-RU'),
        company_id: null,
        pipeline_id: 1,
        setup_date: start,
        event_date: end,
        event_address: "",
        discount_percentage: 0,
        items_json: JSON.stringify(cart)
    };
    
    try {
        const response = await fetch('/api/deals', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (response.ok) {
            alert('Смета успешно сохранена!');
            window.location.href = '/quotes'; // Redirect to quotes list
        } else {
            alert('Ошибка при сохранении');
        }
    } catch(e) {
        alert('Ошибка при сохранении');
    }
}


