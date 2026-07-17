import docx
doc = docx.Document('templates/contract_template.docx')
table = doc.tables[3]
for i, row in enumerate(table.rows):
    print(f"Row {i}: {[c.text for c in row.cells]}")
