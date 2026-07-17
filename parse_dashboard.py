from bs4 import BeautifulSoup

with open('/Users/maximbilyalov/.gemini/antigravity-ide/brain/ec56570b-8318-47db-b8d7-f3292bf0bec0/.system_generated/steps/1727/content.md', 'r') as f:
    text = f.read()

html = text[text.find('<!DOCTYPE html>'):]
soup = BeautifulSoup(html, 'html.parser')

main = soup.find('main', class_='main')

# We just want the inner HTML of main
inner_html = ""
for child in main.children:
    inner_html += child.prettify() if hasattr(child, 'prettify') else str(child)

template = f"""{{% extends "base.html" %}}
{{% block content %}}
{inner_html}
{{% endblock %}}
"""

with open('templates/index.html', 'w') as f:
    f.write(template)

print("Created templates/index.html")
