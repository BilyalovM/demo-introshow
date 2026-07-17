from database import SessionLocal, Equipment, Folder
import json

db = SessionLocal()

# Mock data from intro_show_quotes_new.html
catalog_data = {
    "Микшерные консоли": [
        {"name": "Пульт цифровой Behringer X-Air 18", "price": 15000}
    ],
    "Акустическая система JBL": [
        {"name": "JBL PRX715XLF — активный сабвуфер, 15”", "price": 25000},
        {"name": "JBL PRX115 — активный саттелит, 15”", "price": 25000}
    ],
    "Радио системы ручные\головные\петличные": [
        {"name": "Ручной микрофон SHURE QLXD sm58", "price": 20000},
        {"name": "Shure Headset Mic чёрные", "price": 7500},
        {"name": "Body pack SHURE SLXD", "price": 15000}
    ],
    "Световые консоли и периферия": [
        {"name": "Световой пульт GrandMa on PC", "price": 57000}
    ],
    "Статичные управляемые приборы со сменой цвета на LED источниках света": [
        {"name": "Лед пар 18/10", "price": 7000}
    ],
    "Плазменные и жк панели": [
        {"name": "Плазменная панель размер 49\" 124см", "price": 35000},
        {"name": "Стойки для плазм", "price": 5000}
    ],
    "Конструкция фермы 40x40": [
        {"name": "Сегмент фермы 4х 30\" пунктурный 1м", "price": 4250},
        {"name": "Сегмент фермы 4х 30\" пунктурный 2 м", "price": 8500},
        {"name": "Сегмент фермы 4х 30\" пунктурный 3м", "price": 13000},
        {"name": "Сегмент фермы 4х 30\" пунктурный 4м", "price": 16000},
        {"name": "Конструкции лифтового механизма фермы (основа, каретка, голова) 30\"", "price": 25000},
        {"name": "Комплект ферм для конструкции крыши 30\"", "price": 56000}
    ],
    "Тентовые крыши": [
        {"name": "Крыша 8*8м (серая-чёрная)", "price": 70000}
    ],
    "Электрические цепные подъёмники": [
        {"name": "Лебёдки EXE Rise D8+ 1000kg", "price": 25000},
        {"name": "Пульт для управления лебёдками chain master 1x8", "price": 10000}
    ],
    "Вводные силовые распределительные щиты, силовые кабеля": [
        {"name": "Коммутация", "price": 15000}
    ],
    "Логистика, Тех персонал": [
        {"name": "Грузовая машина", "price": 40000},
        {"name": "Менеджер проекта", "price": 70000},
        {"name": "Звукорежиссёр (инженер звукового пульта)", "price": 50000},
        {"name": "Художник по свету (инженер светового пульта)", "price": 50000},
        {"name": "Техник по свету", "price": 50000},
        {"name": "Техник по сценическим конструкциям", "price": 50000},
        {"name": "Грузчики", "price": 30000},
        {"name": "Батарейки Duracell тип AA", "price": 500}
    ]
}

# Clear existing folders and equipment to avoid duplicates
db.query(Equipment).delete()
db.query(Folder).delete()
db.commit()

# Insert data
for folder_name, items in catalog_data.items():
    folder = Folder(name=folder_name)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    
    for item in items:
        eq = Equipment(
            name=item["name"],
            price=item["price"],
            folder_id=folder.id,
            category=folder_name,
            stock_quantity=2  # ALL TO 2 PCS as requested!
        )
        db.add(eq)
db.commit()
print("Data seeded successfully!")
