import docx
from docxtpl import DocxTemplate
import mammoth
import io

doc = DocxTemplate('templates/contract_template.docx')
context = {"contract_number": "123-A", "company_name": "ООО Тест"}
doc.render(context)
docx_file = io.BytesIO()
doc.save(docx_file)
docx_file.seek(0)
result = mammoth.convert_to_html(docx_file)
print(result.value[:1000])
