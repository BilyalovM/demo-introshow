import docx

doc = docx.Document('templates/contract_template.docx')
for i, table in enumerate(doc.tables):
    if len(table.rows) > 0 and len(table.columns) > 1:
        if 'Арендатор' in table.rows[0].cells[0].text or 'Арендатор' in table.rows[0].cells[1].text:
            print(f"Table {i} has {len(table.rows)} rows:")
            for j, row in enumerate(table.rows):
                row_text = [cell.text.replace('\n', ' ') for cell in row.cells]
                print(f"  Row {j}: {row_text}")
