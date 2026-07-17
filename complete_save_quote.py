from bs4 import BeautifulSoup

with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

js_replace = """
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
"""

html = html.replace('''async function saveQuote() {
    if (cart.length === 0) {
        alert('Добавьте позиции в смету');
        return;
    }
    const start = document.getElementById('rentStart').value;
    const end = document.getElementById('rentEnd').value;
    
    // Create payload
    const payload = {
        title: "Смета от " + new Date().toLocaleDateString('ru-RU'),
        setup_date: start,
        event_date: end,
        company_id: null,
        stage: 1,
        items_json: JSON.stringify(cart)
    };
    
    // We can post to /api/deals
    // Actually the deals API expects form data or json depending on implementation
    // Let's check app.py /api/deals
}''', js_replace)

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
