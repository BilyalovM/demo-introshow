import os
from bs4 import BeautifulSoup

def process_template(scraped_file, template_name, title):
    if not os.path.exists(scraped_file):
        print(f"File {scraped_file} not found.")
        return
    
    with open(scraped_file, 'r', encoding='utf-8') as f:
        html = f.read()
    
    soup = BeautifulSoup(html, 'html.parser')
    main_tag = soup.find('main', class_='main')
    
    if not main_tag:
        print(f"Could not find <main class='main'> in {scraped_file}")
        return
        
    # Create Jinja template
    template_content = f'''{{% extends "base.html" %}}

{{% block title %}}{title} - Intro Show{{% endblock %}}

{{% block content %}}
{main_tag.decode_contents()}
{{% endblock %}}
'''
    with open(f"templates/{template_name}", 'w', encoding='utf-8') as f:
        f.write(template_content)
    print(f"Updated templates/{template_name}")

# Process files
process_template('intro_show_index.html', 'index.html', 'Дашборд')
process_template('intro_show_crm.html', 'crm.html', 'CRM')
process_template('intro_show_inbox.html', 'inbox.html', 'WhatsApp Inbox')
process_template('intro_show_quotes.html', 'quotes.html', 'Сметы и договоры')
process_template('intro_show_tasks.html', 'tasks.html', 'Задачи')
process_template('intro_show_analytics.html', 'analytics.html', 'Аналитика')
