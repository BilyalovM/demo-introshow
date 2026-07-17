import docx
doc = docx.Document('test_output.docx')
table = doc.tables[3]
print(f"Table has {len(table.rows)} rows")
for i, row in enumerate(table.rows):
    row_text = [cell.text.replace('\n', ' ') for cell in row.cells]
    print(f"  Row {i}: {row_text}")
