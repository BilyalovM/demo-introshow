import re

with open("templates/equipment.html", "r", encoding="utf-8") as f:
    html = f.read()

new_load_data = """
        async function loadData() {
            try {
                const [eqRes, fRes] = await Promise.all([
                    fetch('/api/equipment?t=' + new Date().getTime()), 
                    fetch('/api/folders?t=' + new Date().getTime())
                ]);
                if (!eqRes.ok || !fRes.ok) {
                    alert('Ошибка сети: ' + eqRes.status);
                    return;
                }
                equipmentData = await eqRes.json();
                foldersData = await fRes.json();
                renderView();
                populateFolderSelect();
            } catch (err) { 
                alert("Ошибка загрузки данных: " + err.message);
                console.error("Failed to load data", err); 
            }
        }
"""

html = re.sub(r'async function loadData\(\) \{[\s\S]*?\} catch \(err\) \{ console.error\("Failed to load data", err\); \}\n\s*\}', new_load_data.strip(), html)

html = html.replace("window.onload = loadData;", "document.addEventListener('DOMContentLoaded', loadData);")

with open("templates/equipment.html", "w", encoding="utf-8") as f:
    f.write(html)

