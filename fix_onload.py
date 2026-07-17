import re

for filename in ["templates/equipment.html", "templates/quotes_new.html"]:
    with open(filename, "r", encoding="utf-8") as f:
        html = f.read()
    
    html = re.sub(r'document\.addEventListener\([\'"]DOMContentLoaded[\'"], (loadData|loadCatalog)\);', r'\1();', html)
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
