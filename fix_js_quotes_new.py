with open("templates/quotes_new.html", "r", encoding="utf-8") as f:
    html = f.read()

import re

new_js = """
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
"""

html = re.sub(r'async function loadCatalog\(\) \{[\s\S]*?renderCatalog\(\);\n\}', new_js, html)

with open("templates/quotes_new.html", "w", encoding="utf-8") as f:
    f.write(html)
