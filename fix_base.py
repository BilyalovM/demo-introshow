import re

with open("templates/base.html", "r", encoding="utf-8") as f:
    html = f.read()

if "{% block extra_scripts %}" not in html:
    html = html.replace("</body>", "{% block extra_scripts %}{% endblock %}\n</body>")
    with open("templates/base.html", "w", encoding="utf-8") as f:
        f.write(html)
