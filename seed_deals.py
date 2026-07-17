from database import SessionLocal, Deal, DealItem, Equipment, Company
from datetime import datetime, timedelta

db = SessionLocal()

# We need a company
company = db.query(Company).first()
if not company:
    company = Company(name="Айгерим Сапарова")
    db.add(company)
    db.commit()
    db.refresh(company)

# Add some fake deals
deals_data = [
    {"title": "13.05.2025", "event_date": "2026-06-14", "setup_date": "2026-06-14", "sum": 180950, "address": "Презентация Первомайские пруды"},
    {"title": "09.06.2025", "event_date": "2026-06-09", "setup_date": "2026-06-09", "sum": 195030, "address": "Brand Activation промо"},
    {"title": "02.06.2025", "event_date": "2026-06-02", "setup_date": "2026-06-02", "sum": 154440, "address": "Almaty Concerts — Live"},
    {"title": "21.05.2025", "event_date": "2026-05-21", "setup_date": "2026-05-21", "sum": 137390, "address": "Свадьба Lumiere"},
    {"title": "12.05.2025", "event_date": "2026-05-13", "setup_date": "2026-05-13", "sum": 85250, "address": "Корпоратив Halyk"},
]

# Clear existing
db.query(DealItem).delete()
db.query(Deal).delete()
db.commit()

for d in deals_data:
    deal = Deal(
        title=d["title"],
        event_date=d["event_date"],
        setup_date=d["setup_date"],
        final_sum=d["sum"],
        event_address=d["address"],
        company_id=company.id,
        pipeline_id=1,
        stage=1
    )
    db.add(deal)
db.commit()
print("Test deals seeded successfully!")
