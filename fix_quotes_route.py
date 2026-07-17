with open("app.py", "r", encoding="utf-8") as f:
    text = f.read()

# Replace read_quotes
import re
text = re.sub(
    r'@app\.get\("/quotes", response_class=HTMLResponse\)\ndef read_quotes\(request: Request\):\n\s*return templates\.TemplateResponse\("quotes\.html", {"request": request}\)',
    '''@app.get("/quotes", response_class=HTMLResponse)
def read_quotes(request: Request, db: Session = Depends(get_db)):
    deals = db.query(Deal).order_by(Deal.id.desc()).all()
    return templates.TemplateResponse("quotes.html", {"request": request, "deals": deals})''',
    text
)

with open("app.py", "w", encoding="utf-8") as f:
    f.write(text)
