with open("app.py", "r", encoding="utf-8") as f:
    text = f.read()

# Add items_json and setup_date to DealCreate
text = text.replace('''class DealCreate(BaseModel):
    title: str
    company_id: int
    pipeline_id: int
    event_date: str
    event_address: str
    discount_percentage: float = 0.0''', '''class DealCreate(BaseModel):
    title: str
    company_id: Optional[int] = None
    pipeline_id: Optional[int] = 1
    setup_date: Optional[str] = None
    event_date: str
    event_address: Optional[str] = None
    discount_percentage: float = 0.0
    items_json: Optional[str] = None''')

# Update create_deal to handle setup_date and items_json
text = text.replace('''    db_deal = Deal(
        title=deal.title,
        company_id=deal.company_id,
        pipeline_id=deal.pipeline_id,
        event_date=deal.event_date,
        event_address=deal.event_address,
        discount_percentage=deal.discount_percentage,
        stage=stage_id
    )''', '''    db_deal = Deal(
        title=deal.title,
        company_id=deal.company_id,
        pipeline_id=deal.pipeline_id,
        setup_date=deal.setup_date,
        event_date=deal.event_date,
        event_address=deal.event_address,
        discount_percentage=deal.discount_percentage,
        stage=stage_id
    )''')

if "import json" not in text:
    text = "import json\n" + text

text = text.replace('''    history_entry = DealHistory(deal_id=db_deal.id, action_text="Сделка создана")
    db.add(history_entry)
    db.commit()
    return {"id": db_deal.id}''', '''    history_entry = DealHistory(deal_id=db_deal.id, action_text="Сделка создана")
    db.add(history_entry)
    db.commit()

    if deal.items_json:
        try:
            items_list = json.loads(deal.items_json)
            for it in items_list:
                db_item = DealItem(deal_id=db_deal.id, equipment_id=it['id'], quantity=it['qty'], days=it['days'])
                db.add(db_item)
            db.commit()
        except:
            pass

    return {"id": db_deal.id}''')

with open("app.py", "w", encoding="utf-8") as f:
    f.write(text)
print("Updated app.py with Deal items saving.")
