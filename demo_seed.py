"""
Демо-данные CRM: компании, сделки (DEMO:), позиции сметы.

Безопасно для продакшена:
- не удаляет существующие сделки/компании;
- upsert по префиксу заголовка «DEMO:»;
- автозапуск только если deals.count() == 0.
"""
from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database import City, Company, Contact, Deal, DealItem, Equipment, Pipeline, Stage

DEMO_PREFIX = "DEMO: "


def _today_offset(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def _get_or_create_company(db: Session, data: dict) -> Company:
    company = db.query(Company).filter(Company.name == data["name"]).first()
    if company:
        for key in ("director_name", "phone", "email", "address", "bin"):
            if data.get(key) and not getattr(company, key, None):
                setattr(company, key, data[key])
        return company
    company = Company(
        name=data["name"],
        director_name=data.get("director_name"),
        phone=data.get("phone"),
        email=data.get("email"),
        address=data.get("address") or "Алматы",
        bin=data.get("bin"),
    )
    db.add(company)
    db.flush()
    return company


def _pipeline_by_names(db: Session, *names: str) -> Optional[Pipeline]:
    pipes = db.query(Pipeline).order_by(Pipeline.id).all()
    lowered = { (p.name or "").strip().lower(): p for p in pipes }
    for name in names:
        p = lowered.get(name.lower())
        if p:
            return p
    # fallbacks by kind
    for name in names:
        if name.lower() == "лиды":
            return next((p for p in pipes if (p.kind or "") == "lead"), None)
        if name.lower() in ("аренда", "продажа"):
            kind_deals = [p for p in pipes if (p.kind or "deal") == "deal"]
            if name.lower() == "аренда" and kind_deals:
                return kind_deals[0]
            if name.lower() == "продажа" and len(kind_deals) > 1:
                return kind_deals[1]
    return pipes[0] if pipes else None


def _stage_id(db: Session, pipeline: Pipeline, *stage_names: str) -> Optional[int]:
    if not pipeline:
        return None
    stages = (
        db.query(Stage)
        .filter(Stage.pipeline_id == pipeline.id)
        .order_by(Stage.order_index, Stage.id)
        .all()
    )
    by_name = { (s.name or "").strip().lower(): s for s in stages }
    for name in stage_names:
        st = by_name.get(name.lower())
        if st:
            return st.id
    return stages[0].id if stages else None


def _ensure_demo_equipment(db: Session) -> dict:
    """Минимальный каталог для позиций демо-смет (если склада мало/пусто)."""
    catalog = [
        ("Пульт цифровой Behringer X-Air 18", "Микшерные консоли", 15000, "own", None, 4),
        ("JBL PRX715XLF — активный сабвуфер, 15”", "Акустическая система JBL", 25000, "own", None, 6),
        ("JBL PRX115 — активный саттелит, 15”", "Акустическая система JBL", 25000, "own", None, 8),
        ("Ручной микрофон SHURE QLXD sm58", "Радио системы", 20000, "own", None, 10),
        ("Световой пульт GrandMa on PC", "Световые консоли", 57000, "own", None, 2),
        ("Лед пар 18/10", "Свет LED", 7000, "own", None, 20),
        ("Плазменная панель размер 49\" 124см", "Экраны", 35000, "own", None, 4),
        ("Стойки для плазм", "Экраны", 5000, "own", None, 8),
        ("Грузовая машина", "Логистика, Тех персонал", 40000, "own", None, 2),
        ("Звукорежиссёр (инженер звукового пульта)", "Логистика, Тех персонал", 50000, "own", None, 5),
        ("Техник по свету", "Логистика, Тех персонал", 50000, "own", None, 8),
        ("Грузчики", "Логистика, Тех персонал", 30000, "own", None, 10),
        ("LED экран P3.9 (субаренда)", "Экраны", 45000, "subrental", "EventPro Almaty", 10),
        ("Линейный массив 2×top (субаренда)", "Звук", 80000, "subrental", "SoundRent", 4),
        ("Дым-машина hazer (субаренда)", "Свет", 12000, "subrental", "LightHub", 6),
    ]
    by_key = {}
    for name, cat, price, wh, supplier, qty in catalog:
        eq = db.query(Equipment).filter(Equipment.name == name).first()
        if not eq:
            eq = Equipment(
                name=name,
                category=cat,
                price=float(price),
                cost_price=float(price) * 0.65 if wh == "subrental" else 0.0,
                stock_quantity=int(qty),
                status="Доступно",
                warehouse_type=wh,
                supplier=supplier,
                description="Демо-позиция каталога",
            )
            db.add(eq)
            db.flush()
        key = name.split("(")[0].strip().lower()
        by_key[name] = eq
        by_key[key] = eq
    db.flush()
    return by_key


def _eq(eq_map: dict, *names: str) -> Optional[Equipment]:
    for name in names:
        if name in eq_map:
            return eq_map[name]
        low = name.lower()
        for k, v in eq_map.items():
            if isinstance(k, str) and low in k.lower():
                return v
    return None


def _add_items(db: Session, deal: Deal, lines: list, eq_map: dict) -> float:
    """lines: (name_hints, qty, days, price_override?). Returns sum."""
    total = 0.0
    for line in lines:
        hints = line[0] if isinstance(line[0], (list, tuple)) else [line[0]]
        qty = int(line[1])
        days = int(line[2])
        price_override = line[3] if len(line) > 3 else None
        eq = _eq(eq_map, *hints)
        if not eq:
            continue
        price = float(price_override if price_override is not None else (eq.price or 0))
        db.add(DealItem(
            deal_id=deal.id,
            equipment_id=eq.id,
            quantity=qty,
            days=days,
            price=price,
            subrental_status="reserved" if (eq.warehouse_type or "") == "subrental" else None,
        ))
        total += price * qty * days
    return total


def seed_demo_deals(db: Session, *, only_if_empty: bool = False) -> dict:
    """
    Создаёт недостающие демо-сделки с префиксом DEMO:.
    only_if_empty=True — выйти, если в БД уже есть любые сделки (cold start).
    """
    existing_count = db.query(Deal).count()
    if only_if_empty and existing_count > 0:
        return {
            "status": "skipped",
            "reason": "deals_not_empty",
            "deals_total": existing_count,
            "created": 0,
            "skipped_existing": 0,
        }

    eq_map = _ensure_demo_equipment(db)

    companies_data = [
        {"name": "TechConf Astana", "director_name": "Нурлан Оспанов", "phone": "+77011234504", "email": "nurlan@techconf.kz", "bin": "120140001234"},
        {"name": "Event Production KZ", "director_name": "Айгерим Сапарова", "phone": "+77015541380", "email": "aigerim@eventprod.kz", "bin": "090540005678"},
        {"name": "Festival Group", "director_name": "Ержан Мукан", "phone": "+77029876543", "email": "erzhan@festgroup.kz"},
        {"name": "Almaty Concerts", "director_name": "Тимур Ахметов", "phone": "+77071112233", "email": "timur@almatyconcerts.kz"},
        {"name": "Wedding Studio Lumiere", "director_name": "Жанна Ким", "phone": "+77054445566", "email": "zhanna@lumiere.kz"},
        {"name": "Brand Activation", "director_name": "Динара Беку", "phone": "+77089998877", "email": "dinara@brandact.kz"},
        {"name": "Corporate Events", "director_name": "Sandugash B.", "phone": "+77013334455", "email": "sandugash@corpevents.kz"},
        {"name": "Retail Media KZ", "director_name": "Алишер Н.", "phone": "+77070001122", "email": "ali@retailmedia.kz"},
    ]
    companies = {c["name"]: _get_or_create_company(db, c) for c in companies_data}
    db.flush()

    # primary contacts
    for cname, contact_name, phone in [
        ("TechConf Astana", "Нурлан Оспанов", "+77011234504"),
        ("Brand Activation", "Динара Беку", "+77089998877"),
        ("Almaty Concerts", "Тимур Ахметов", "+77071112233"),
    ]:
        company = companies[cname]
        exists = (
            db.query(Contact)
            .filter(Contact.company_id == company.id, Contact.name == contact_name)
            .first()
        )
        if not exists:
            db.add(Contact(
                name=contact_name,
                phone=phone,
                company_id=company.id,
                is_primary=True,
                position="Директор",
            ))
    db.flush()

    rental = _pipeline_by_names(db, "Аренда", "Основная воронка")
    leads = _pipeline_by_names(db, "Лиды")
    sales = _pipeline_by_names(db, "Продажа", "Продажи")

    demos = [
        {
            "title": f"{DEMO_PREFIX}Конференция TechConf Astana",
            "company": "TechConf Astana",
            "pipeline": rental,
            "stage_names": ("Согласование сметы",),
            "setup": _today_offset(5),
            "event": _today_offset(7),
            "address": "Астана, Expo Congress Hall",
            "city": "Астана",
            "shifts": 2,
            "source": "referral",
            "comment": "Демо: аренда звука/света + персонал",
            "items": [
                (["Behringer", "X-Air"], 1, 2),
                (["PRX715", "сабвуфер"], 2, 2),
                (["PRX115", "саттелит"], 4, 2),
                (["микрофон", "SHURE"], 4, 2),
                (["Звукорежиссёр"], 1, 2),
                (["Грузовая машина"], 1, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}КП для Event Production KZ",
            "company": "Event Production KZ",
            "pipeline": rental,
            "stage_names": ("Договор и счет",),
            "setup": _today_offset(10),
            "event": _today_offset(11),
            "address": "Алматы, Atakent",
            "city": "Алматы",
            "shifts": 1,
            "source": "whatsapp",
            "comment": "Демо: договор на стадии согласования",
            "items": [
                (["GrandMa", "Световой пульт"], 1, 1),
                (["Лед пар"], 12, 1),
                (["Техник по свету"], 2, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Фестиваль Festival Group",
            "company": "Festival Group",
            "pipeline": rental,
            "stage_names": ("Первичный контакт",),
            "setup": _today_offset(20),
            "event": _today_offset(22),
            "address": "Алматы, Medeu",
            "city": "Алматы",
            "shifts": 3,
            "source": "instagram",
            "comment": "Демо: крупный фестиваль, ранний лид в аренде",
            "items": [
                (["PRX715"], 4, 3),
                (["PRX115"], 8, 3),
                (["Линейный массив"], 2, 3),
                (["Генератор", "LED экран"], 1, 3),
                (["Грузчики"], 4, 3),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Свет для Almaty Concerts",
            "company": "Almaty Concerts",
            "pipeline": rental,
            "stage_names": ("Первичный контакт",),
            "setup": _today_offset(3),
            "event": _today_offset(3),
            "address": "Алматы, Republic Palace",
            "city": "Алматы",
            "shifts": 1,
            "source": "site",
            "comment": "Демо: концертный свет",
            "items": [
                (["GrandMa"], 1, 1),
                (["Лед пар"], 16, 1),
                (["Дым-машина", "hazer"], 2, 1),
                (["Техник по свету"], 2, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Свадьба Lumiere",
            "company": "Wedding Studio Lumiere",
            "pipeline": rental,
            "stage_names": ("Успешно реализовано", "Отгружено / закрыто"),
            "setup": _today_offset(-14),
            "event": _today_offset(-13),
            "address": "Алматы, Rixos",
            "city": "Алматы",
            "shifts": 1,
            "source": "referral",
            "comment": "Демо: закрытая свадьба",
            "items": [
                (["плазм", "панель"], 2, 1),
                (["микрофон"], 2, 1),
                (["Лед пар"], 8, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Промо Brand Activation",
            "company": "Brand Activation",
            "pipeline": rental,
            "stage_names": ("Согласование сметы",),
            "setup": _today_offset(8),
            "event": _today_offset(9),
            "address": "Алматы, Dostyk Plaza",
            "city": "Алматы",
            "shifts": 2,
            "source": "instagram",
            "comment": "Демо: субаренда LED + свой звук",
            "items": [
                (["LED экран P3.9"], 1, 2),
                (["Behringer"], 1, 2),
                (["микрофон"], 2, 2),
                (["Грузовая машина"], 1, 1),
                (["Грузчики"], 2, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Корпоратив Halyk (Corporate Events)",
            "company": "Corporate Events",
            "pipeline": rental,
            "stage_names": ("Сделка проиграна", "Отказ"),
            "setup": _today_offset(-3),
            "event": _today_offset(-2),
            "address": "Алматы, отель Казахстан",
            "city": "Алматы",
            "shifts": 1,
            "source": "other",
            "comment": "Демо: проигранная сделка",
            "loss_reason": "Выбрали другого подрядчика",
            "items": [
                (["PRX115"], 2, 1),
                (["микрофон"], 2, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Лид: Retail Media — запрос с сайта",
            "company": "Retail Media KZ",
            "pipeline": leads or rental,
            "stage_names": ("Новый лид", "В работе", "Первичный контакт"),
            "setup": _today_offset(15),
            "event": _today_offset(16),
            "address": "Алматы",
            "city": "Алматы",
            "shifts": 1,
            "source": "site",
            "qualification": "rental",
            "comment": "Демо: лид из публичной формы",
            "items": [
                (["плазм"], 1, 1),
            ],
        },
        {
            "title": f"{DEMO_PREFIX}Продажа LED-панелей Retail Media",
            "company": "Retail Media KZ",
            "pipeline": sales or rental,
            "stage_names": ("КП / согласование", "Согласование сметы", "Первичный контакт"),
            "setup": _today_offset(12),
            "event": _today_offset(12),
            "address": "Алматы, склад клиента",
            "city": "Алматы",
            "shifts": 1,
            "source": "manual",
            "qualification": "sale",
            "comment": "Демо: воронка Продажа",
            "items": [
                (["плазм", "панель"], 4, 1, 320000),
                (["Стойки для плазм", "стойк"], 4, 1, 25000),
            ],
        },
    ]

    created = 0
    skipped = 0
    created_titles = []
    almaty = db.query(City).filter(City.slug == "almaty").first()

    for spec in demos:
        title = spec["title"]
        exists = db.query(Deal).filter(Deal.title == title).first()
        if exists:
            # дотянуть city_id у старых демо-сделок
            if almaty and getattr(exists, "city_id", None) is None:
                exists.city_id = almaty.id
            skipped += 1
            continue
        pipe = spec["pipeline"]
        if not pipe:
            skipped += 1
            continue
        stage_id = _stage_id(db, pipe, *spec["stage_names"])
        company = companies[spec["company"]]
        deal = Deal(
            title=title,
            company_id=company.id,
            pipeline_id=pipe.id,
            stage=stage_id or 1,
            setup_date=spec["setup"],
            event_date=spec["event"],
            event_address=spec["address"],
            city=spec.get("city") or (almaty.name if almaty else "Алматы"),
            city_id=almaty.id if almaty else None,
            shifts=float(spec.get("shifts") or 1),
            discount_percentage=0.0,
            tax_percentage=16.0,
            comment=spec.get("comment"),
            source=spec.get("source"),
            loss_reason=spec.get("loss_reason"),
            qualification=spec.get("qualification"),
            is_qualified=bool(spec.get("qualification")),
        )
        db.add(deal)
        db.flush()
        total = _add_items(db, deal, spec.get("items") or [], eq_map)
        # налог 16% сверху как в калькуляторе упрощённо
        deal.final_sum = round(total * 1.16, 2)
        created += 1
        created_titles.append(title)

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "skipped_existing": skipped,
        "deals_total": db.query(Deal).count(),
        "titles": created_titles,
        "prefix": DEMO_PREFIX.strip(),
    }
