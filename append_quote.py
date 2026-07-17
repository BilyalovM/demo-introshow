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
                    if (deal.setup_date) dateInputs[1].value = deal.setup_date;
                    if (deal.event_date) dateInputs[2].value = deal.event_date;
                }
                
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
</script>
"""

html = html.replace("</script>", script)

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
