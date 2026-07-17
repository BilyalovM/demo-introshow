import re

with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

script = """
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
"""

if "{% if quote_id %}" not in html:
    html = html.replace("window.onload = loadCatalog;", "document.addEventListener('DOMContentLoaded', loadCatalog);\n" + script)
    html = html.replace("document.addEventListener(\"DOMContentLoaded\", loadCatalog);", "document.addEventListener(\"DOMContentLoaded\", loadCatalog);\n" + script)

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
