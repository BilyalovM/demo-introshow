import docx
doc = docx.Document('templates/contract_template.docx')
table_equipment = doc.tables[3]
# Find the loop row
for i, row in enumerate(table_equipment.rows):
    if '{% tr for item in items %}' in row.cells[0].text:
        row.cells[0].text = row.cells[0].text.replace('{% tr for item in items %}', '{% for item in items %}')
        row.cells[-1].text = row.cells[-1].text.replace('{% tr endfor %}', '{% endfor %}')
doc.save('templates/contract_template.docx')
