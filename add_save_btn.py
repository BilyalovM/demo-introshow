from bs4 import BeautifulSoup
import re

with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

html = html.replace('<button class="btn btn-green">Сохранить в базу</button>', '<button class="btn btn-green" onclick="saveQuote()">Сохранить в базу</button>')

js_add = """
async function saveQuote() {
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
}
"""

if "async function saveQuote" not in html:
    html = html.replace('</script>', js_add + '</script>')

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
