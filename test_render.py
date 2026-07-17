import os
from document_generator import generate_contract
context = {
    "contract_number": "123",
    "contract_date": "10.10.2026",
    "company_name": "ООО Ромашка",
    "director_name": "Иванов И.И.",
    "iin_bin": "123456789",
    "iban": "KZ123",
    "based_on": "Устава",
    "company_address": "ул. Пушкина",
    "bank_name": "Kaspi",
    "kbe": "17",
    "bik": "CASP",
    "event_name": "Test Event",
    "event_date": "20.10.2026",
    "event_address": "Almaty",
    "items": [{"name": "Item 1", "quantity": 1, "price_text": "100", "days": 1, "subtotal_text": "100"}],
    "equipment_total_text": "100",
    "fixed_total": 0,
    "grand_total": 100,
    "grand_total_text": "Сто тенге",
    "discount_percentage": 0
}
generate_contract(context, "templates/contract_template.docx", "test_output.docx")
print("Render successful.")
