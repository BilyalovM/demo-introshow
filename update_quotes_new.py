from bs4 import BeautifulSoup

with open('intro_show_quotes_new.html', 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'html.parser')
main_tag = soup.find('main', class_='main')

if main_tag:
    template_content = f'''{{% extends "base.html" %}}

{{% block title %}}Новая смета - Intro Show{{% endblock %}}

{{% block content %}}
{main_tag.decode_contents()}
{{% endblock %}}
'''
    with open('templates/quotes_new.html', 'w', encoding='utf-8') as f:
        f.write(template_content)
    print('Created templates/quotes_new.html')
else:
    print('No main tag found.')
