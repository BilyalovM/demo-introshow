from bs4 import BeautifulSoup
import re

with open('/Users/maximbilyalov/.gemini/antigravity-ide/brain/ec56570b-8318-47db-b8d7-f3292bf0bec0/.system_generated/steps/1727/content.md', 'r') as f:
    text = f.read()

html = text[text.find('<!DOCTYPE html>'):]

soup = BeautifulSoup(html, 'html.parser')

sidebar = soup.find('aside', class_='sidebar')
topbar = soup.find('header', class_='topbar')
fab = soup.find('button', class_='copilot-fab')
bottom_nav = soup.find('nav', class_='bottom-nav')

# Prettify the HTML
sidebar_html = sidebar.prettify() if sidebar else ''
topbar_html = topbar.prettify() if topbar else ''
fab_html = fab.prettify() if fab else ''
bottom_nav_html = bottom_nav.prettify() if bottom_nav else ''

# Replace active classes logic in Jinja
sidebar_html = sidebar_html.replace('href="/"', 'href="{{ url_for(\'index\') }}"')
sidebar_html = sidebar_html.replace('href="/crm"', 'href="{{ url_for(\'crm\') }}"')
sidebar_html = sidebar_html.replace('href="/quotes"', 'href="{{ url_for(\'quotes\') }}"')
sidebar_html = sidebar_html.replace('href="/settings/parties"', 'href="{{ url_for(\'settings\') }}"')
# Keep the active class dynamic for sidebar
sidebar_html = sidebar_html.replace('class="nav-link active"', 'class="nav-link"')
sidebar_html = sidebar_html.replace('href="{{ url_for(\'index\') }}"', 'href="{{ url_for(\'index\') }}" class="nav-link {% if request.url.path == \'/\' %}active{% endif %}"')
sidebar_html = sidebar_html.replace('href="{{ url_for(\'crm\') }}"', 'href="{{ url_for(\'crm\') }}" class="nav-link {% if request.url.path == \'/crm\' %}active{% endif %}"')
sidebar_html = sidebar_html.replace('href="{{ url_for(\'quotes\') }}"', 'href="{{ url_for(\'quotes\') }}" class="nav-link {% if request.url.path == \'/quotes\' %}active{% endif %}"')

# Also add equipment and clients to sidebar since intro_showstudio didn't have them explicitly mapped
# Intro Show has "Задачи" (/tasks) -> Replace with Склад
sidebar_html = sidebar_html.replace('href="/tasks"', 'href="{{ url_for(\'equipment\') }}" class="nav-link {% if request.url.path == \'/equipment\' %}active{% endif %}"')
sidebar_html = sidebar_html.replace('Задачи', 'Склад')

# Intro Show has "Аналитика" (/analytics) -> Replace with Клиенты
sidebar_html = sidebar_html.replace('href="/analytics"', 'href="{{ url_for(\'companies\') }}" class="nav-link {% if request.url.path == \'/companies\' %}active{% endif %}"')
sidebar_html = sidebar_html.replace('Аналитика', 'Клиенты')

bottom_nav_html = bottom_nav_html.replace('href="/"', 'href="{{ url_for(\'index\') }}"')
bottom_nav_html = bottom_nav_html.replace('href="/crm"', 'href="{{ url_for(\'crm\') }}"')
bottom_nav_html = bottom_nav_html.replace('href="/quotes"', 'href="{{ url_for(\'quotes\') }}"')

# Insert into a base.html template
base_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5, viewport-fit=cover" />
    <title>Intro Show — premium workspace</title>
    <link rel="stylesheet" href="{{{{ url_for('static', path='css/dashboard.css') }}}}" />
    {{% block extra_head %}}{{% endblock %}}
</head>
<body>
    <div class="app">
        {sidebar_html}
        <div class="content">
            {topbar_html}
            <main class="main">
                {{% block content %}}{{% endblock %}}
            </main>
        </div>
    </div>
    {fab_html}
    {bottom_nav_html}
</body>
</html>
"""

with open('templates/base.html', 'w') as f:
    f.write(base_template)

print("Created templates/base.html")
