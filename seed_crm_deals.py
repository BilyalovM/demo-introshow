from database import SessionLocal, Deal, Company, Stage, Pipeline, init_db
import datetime

db = SessionLocal()

# Ensure default stages exist
init_db()

# Clear existing companies and deals to start fresh
db.query(Deal).delete()
db.query(Company).delete()
db.commit()

# Create companies
companies_data = [
    {"name": "TechConf Astana", "director_name": "Нурлан Оспанов", "phone": "+77011234504", "email": "nurlan@techconf.kz"},
    {"name": "Event Production KZ", "director_name": "Айгерим Сапарова", "phone": "+77015541380", "email": "aigerim@eventprod.kz"},
    {"name": "Festival Group", "director_name": "Ержан Мукан", "phone": "+77029876543", "email": "erzhan@festgroup.kz"},
    {"name": "Almaty Concerts", "director_name": "Тимур Ахметов", "phone": "+77071112233", "email": "timur@almatyconcerts.kz"},
    {"name": "Wedding Studio Lumiere", "director_name": "Жанна Ким", "phone": "+77054445566", "email": "zhanna@lumiere.kz"},
    {"name": "Brand Activation", "director_name": "Динара Беку", "phone": "+77089998877", "email": "dinara@brandact.kz"},
    {"name": "Corporate Events", "director_name": "Sandugash B.", "phone": "+77013334455", "email": "sandugash@corpevents.kz"},
]

companies = []
for c_data in companies_data:
    company = Company(
        name=c_data["name"],
        director_name=c_data["director_name"],
        phone=c_data["phone"],
        email=c_data["email"],
        address="Алматы"
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    companies.append(company)

# Get stages
stages = db.query(Stage).order_by(Stage.order_index).all()
stage_map = {s.name: s.id for s in stages}

# Deals
deals_data = [
    {"title": "Конференция TechConf Astana", "company_id": companies[0].id, "stage": stage_map.get("Согласование сметы", 2), "sum": 2150000},
    {"title": "КП для Event Production KZ", "company_id": companies[1].id, "stage": stage_map.get("Договор и счет", 3), "sum": 1225070},
    {"title": "Фестиваль Festival Group", "company_id": companies[2].id, "stage": stage_map.get("Первичный контакт", 1), "sum": 3400000},
    {"title": "Свет для Almaty Concerts", "company_id": companies[3].id, "stage": stage_map.get("Первичный контакт", 1), "sum": 860000},
    {"title": "Свадьба Lumiere", "company_id": companies[4].id, "stage": stage_map.get("Успешно реализовано", 6), "sum": 450000},
    {"title": "Промо Brand Activation", "company_id": companies[5].id, "stage": stage_map.get("Согласование сметы", 2), "sum": 900000},
    {"title": "Корпоратив Halyk (Corporate Events)", "company_id": companies[6].id, "stage": stage_map.get("Сделка проиграна", 7), "sum": 300000},
]

for d_data in deals_data:
    deal = Deal(
        title=d_data["title"],
        company_id=d_data["company_id"],
        pipeline_id=1,
        stage=d_data["stage"],
        final_sum=d_data["sum"],
        event_date=(datetime.date.today() + datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
        setup_date=datetime.date.today().strftime("%Y-%m-%d"),
        event_address="Алматы, Отель Казахстан",
        comment="Интегрированная сделка"
    )
    db.add(deal)
db.commit()
print("CRM Deals seeded successfully!")
