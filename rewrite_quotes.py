with open("templates/quotes.html", "r", encoding="utf-8") as f:
    html = f.read()

import re

# Remove the static rows and replace with Jinja block
tbody_start = html.find('<tbody>')
tbody_end = html.find('</tbody>') + len('</tbody>')

jinja_tbody = """<tbody>
{% for deal in deals %}
<tr>
    <td class="card-head"><a href="/quotes/{{ deal.id }}" style="font-weight:700">№ {{ deal.title }}</a></td>
    <td data-label="Дата">{{ deal.setup_date }} - {{ deal.event_date }}</td>
    <td data-label="Арендатор">{% if deal.company %}{{ deal.company.name }}{% else %}-{% endif %}</td>
    <td data-label="Мероприятие">{{ deal.event_address or '' }}</td>
    <td class="num" data-label="Итого ₸"><strong>{{ deal.final_sum }}</strong></td>
    <td class="num" data-label=""><a class="btn btn-sm" href="/quotes/{{ deal.id }}">Открыть</a></td>
</tr>
{% endfor %}
{% if not deals %}
<tr><td colspan="6" style="text-align:center; color:gray; padding:20px;">Нет созданных смет</td></tr>
{% endif %}
</tbody>"""

new_html = html[:tbody_start] + jinja_tbody + html[tbody_end:]
new_html = re.sub(r'Всего <!-- -->\d+<!-- --> документов на сумму <!-- -->[0-9\s ]+<!-- --> ₸.', 
                  'Всего документов: {{ deals|length }}', new_html)

with open("templates/quotes.html", "w", encoding="utf-8") as f:
    f.write(new_html)
