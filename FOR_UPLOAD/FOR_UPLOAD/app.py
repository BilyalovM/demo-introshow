from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, File, UploadFile, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict
import json
import os
import tempfile
import requests
# import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
if os.getenv("GEMINI_API_KEY"):
    # genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

from sqlalchemy.orm import Session
from database import init_db, get_db, Equipment, Company, Deal, DealItem, CustomField, DealFieldValue, DealHistory, Project2D, Folder, Pipeline, Stage, PushSubscription, User, engine
from sqlalchemy import text, func

from calculator import calculate_estimate
from document_generator import generate_contract, get_rubles_text
import notifications

# Initialize the database
init_db()

# DB Migration & Seeding
with Session(engine) as session:
    # 1. Add pipeline_id to deals if it doesn't exist
    try:
        session.execute(text("ALTER TABLE deals ADD COLUMN pipeline_id INTEGER"))
        session.commit()
    except Exception:
        session.rollback() # column already exists
        
    # 2. Seed main pipeline if empty
    default_pipeline = session.query(Pipeline).first()
    if not default_pipeline:
        default_pipeline = Pipeline(name="Основная воронка")
        session.add(default_pipeline)
        session.commit()
        session.refresh(default_pipeline)
        
        # Create default stages
        default_stages = [
            "Первичный контакт", "Согласование сметы", "Договор и счет",
            "Предоплата внесена", "Монтаж / Мероприятие", "Успешно реализовано",
            "Сделка проиграна"
        ]
        for i, stage_name in enumerate(default_stages):
            st = Stage(pipeline_id=default_pipeline.id, name=stage_name, order_index=i+1)
            session.add(st)
        session.commit()
        
    # 3. Update existing deals to the default pipeline if they are null
    session.execute(text(f"UPDATE deals SET pipeline_id = {default_pipeline.id} WHERE pipeline_id IS NULL"))
    session.commit()

    # 4. Add AI & 3D properties and custom fields to equipment
    try:
        session.execute(text("ALTER TABLE equipment ADD COLUMN weight FLOAT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN dimensions TEXT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN power_w FLOAT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN dispersion TEXT"))
        session.commit()
    except Exception:
        session.rollback() # Columns already exist

    try:
        session.execute(text("ALTER TABLE equipment ADD COLUMN custom_fields JSON"))
        session.commit()
    except Exception:
        session.rollback()
        
    try:
        session.execute(text("ALTER TABLE companies ADD COLUMN telegram_chat_id VARCHAR"))
        session.commit()
    except Exception:
        session.rollback()

    # 5. Create default admin user
    try:
        if session.query(User).count() == 0:
            import hashlib
            default_password = hashlib.sha256("admin".encode()).hexdigest()
            admin_user = User(username="admin", hashed_password=default_password, role="admin")
            session.add(admin_user)
            session.commit()
    except Exception:
        session.rollback()

app = FastAPI(title="Rental Business Automation")

def expand_company_type(name: str) -> str:
    from datetime import datetime
    if not name:
        return name
    name = name.strip()
    lower_name = name.lower()
    if lower_name.startswith("тоо "):
        return "Товарищество с ограниченной ответственностью " + name[4:]
    if lower_name.startswith("ип "):
        return "Индивидуальный предприниматель " + name[3:]
    if lower_name.startswith("ао "):
        return "Акционерное общество " + name[3:]
    return name

# Set up templates
templates = Jinja2Templates(directory="templates")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
# Models for the API
class EquipmentItem(BaseModel):
    name: str
    price: float
    quantity: int
    days: int
    category_type: str  # 'equipment' or 'fixed'

class LayoutOptimizationRequest(BaseModel):
    items: List[EquipmentItem]
    width: float
    length: float

# Additional Pydantic models for DB operations
class EquipmentCreate(BaseModel):
    name: str
    category: str
    price: float
    stock_quantity: int
    status: str
    folder_id: Optional[int] = None
    description: Optional[str] = None
    weight: Optional[float] = None
    dimensions: Optional[str] = None
    power_w: Optional[float] = None
    dispersion: Optional[str] = None
    custom_fields: Optional[Dict[str, str]] = {}
    
class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None

class DealCreate(BaseModel):
    title: str
    company_id: Optional[int] = None
    pipeline_id: Optional[int] = 1
    setup_date: Optional[str] = None
    event_date: str
    event_address: Optional[str] = None
    discount_percentage: float = 0.0
    items_json: Optional[str] = None

class DealStageUpdate(BaseModel):
    stage: int
    pipeline_id: Optional[int] = None

class CompanyCreate(BaseModel):
    name: str
    bin: str
    director_name: str
    phone: str
    email: str
    requisites: str
    based_on: str = "Устава"
    address: str = ""
    bank_name: str = ""
    kbe: str = ""
    bik: str = ""

class CustomFieldCreate(BaseModel):
    name: str
    field_type: str

class DealFieldValueUpdate(BaseModel):
    field_id: int
    value: str

class DealFieldUpdateList(BaseModel):
    fields: List[DealFieldValueUpdate]

class Project2DSave(BaseModel):
    layout_data_json: str

class PipelineCreate(BaseModel):
    name: str

class StageCreate(BaseModel):
    name: str
    order_index: int
    is_active_rent: Optional[bool] = False

import hashlib

def verify_password(plain_password, hashed_password):
    return hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password

def get_password_hash(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user(request: Request, db: Session = Depends(get_db)):
    user = db.query(User).first()
    return user

# -----------------
# FRONTEND ROUTES
# -----------------

@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return JSONResponse(status_code=400, content={"error": "Invalid username or password"})
    
    # Very simple session: just use username as token for now (since we don't have JWT configured)
    response.set_cookie(key="session_token", value=user.username, httponly=True)
    return {"status": "success"}

@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session_token")
    return {"status": "success"}


@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})

@app.get("/quotes/new", response_class=HTMLResponse)
def read_quotes_new(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("quotes_new.html", {"request": request})

@app.get("/quotes/{quote_id:int}", response_class=HTMLResponse)
def read_quote_detail(request: Request, quote_id: int, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("quotes_new.html", {"request": request, "quote_id": quote_id})

@app.get("/quotes", response_class=HTMLResponse)
def read_quotes(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deals = db.query(Deal).order_by(Deal.id.desc()).all()
    return templates.TemplateResponse("quotes.html", {"request": request, "active_page": "quotes", "deals": deals})

@app.get("/inbox", response_class=HTMLResponse)
async def read_inbox(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("inbox.html", {"request": request, "active_page": "inbox"})

@app.get("/tasks", response_class=HTMLResponse)
async def read_tasks(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("tasks.html", {"request": request, "active_page": "tasks"})

@app.get("/analytics", response_class=HTMLResponse)
async def read_analytics(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("analytics.html", {"request": request, "active_page": "analytics"})

@app.get("/companies", response_class=HTMLResponse)
async def read_companies(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("companies.html", {"request": request, "active_page": "companies"})

@app.get("/users", response_class=HTMLResponse)
async def read_users(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return HTMLResponse("Access denied", status_code=403)
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "settings"})

@app.get("/api/users")
def get_users(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role} for u in users]

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

@app.post("/api/users")
def create_user(u: UserCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if db.query(User).count() >= 10:
        return JSONResponse(status_code=400, content={"error": "Максимальное количество пользователей (10) достигнуто"})
    
    existing = db.query(User).filter(User.username == u.username).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Пользователь уже существует"})
        
    new_user = User(username=u.username, hashed_password=get_password_hash(u.password), role=u.role)
    db.add(new_user)
    db.commit()
    return {"status": "success"}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if current_user.id == user_id:
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить самого себя"})
        
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        db.delete(u)
        db.commit()
    return {"status": "success"}

@app.get("/settings", response_class=HTMLResponse)
async def read_settings(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})

@app.get("/assistant", response_class=HTMLResponse)
async def read_assistant(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("assistant.html", {"request": request, "active_page": "assistant"})

@app.get("/equipment", response_class=HTMLResponse)
async def read_equipment(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("equipment.html", {"request": request, "active_page": "equipment"})

@app.get("/crm", response_class=HTMLResponse)
async def read_crm(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("crm.html", {"request": request, "active_page": "crm"})

@app.get("/tracking/{deal_id}", response_class=HTMLResponse)
def read_tracking(request: Request, deal_id: int, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        return HTMLResponse("Заказ не найден", status_code=404)
    return templates.TemplateResponse("tracking.html", {"request": request, "deal": deal})

# -----------------
# API ROUTES
# -----------------

# -- Folders --
@app.get("/api/folders")
def get_folders(db: Session = Depends(get_db)):
    return db.query(Folder).all()

@app.post("/api/folders")
def create_folder(f: FolderCreate, db: Session = Depends(get_db)):
    new_f = Folder(name=f.name, parent_id=f.parent_id)
    db.add(new_f)
    db.commit()
    db.refresh(new_f)
    return new_f

# -- Equipment --
@app.get("/api/equipment")
def get_equipment(db: Session = Depends(get_db)):
    equip_list = db.query(Equipment).all()
    
    # Calculate rented quantities based on stages
    rented_counts = db.query(
        DealItem.equipment_id, 
        func.sum(DealItem.quantity).label('total_rented')
    ).join(Deal, Deal.id == DealItem.deal_id)\
     .join(Stage, Deal.stage == Stage.id)\
     .filter(Stage.is_active_rent == True)\
     .group_by(DealItem.equipment_id).all()
     
    rented_map = {row.equipment_id: row.total_rented for row in rented_counts}
    
    result = []
    for eq in equip_list:
        rented = rented_map.get(eq.id, 0)
        stock = eq.stock_quantity or 0
        
        # Build dictionary
        eq_dict = {
            "id": eq.id,
            "name": eq.name,
            "category": eq.category,
            "price": eq.price,
            "stock_quantity": stock,
            "status": eq.status,
            "folder_id": eq.folder_id,
            "description": eq.description,
            "photo_url": eq.photo_url,
            "custom_fields": eq.custom_fields,
            "rented_quantity": rented,
            "available_quantity": stock - rented
        }
        result.append(eq_dict)
        
    return result

class EnrichRequest(BaseModel):
    name: str



class AISettingsUpdate(BaseModel):
    api_key: str

class TGTokenUpdate(BaseModel):
    token: str

@app.post("/api/settings/telegram")
def update_tg_settings(settings: TGTokenUpdate, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
        
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
            
    with open(env_path, "w") as f:
        key_found = False
        for line in lines:
            if line.startswith("TG_BOT_TOKEN="):
                f.write(f"TG_BOT_TOKEN={settings.token}\n")
                key_found = True
            else:
                f.write(line)
        if not key_found:
            f.write(f"TG_BOT_TOKEN={settings.token}\n")
            
    os.environ["TG_BOT_TOKEN"] = settings.token
    # Also update notifications module memory if needed
    notifications.TG_TOKEN = settings.token
    return {"status": "success"}

@app.post("/api/settings/ai")
def update_ai_settings(settings: AISettingsUpdate):
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
            
    with open(env_path, "w") as f:
        key_found = False
        for line in lines:
            if line.startswith("GEMINI_API_KEY="):
                f.write(f"GEMINI_API_KEY={settings.api_key}\n")
                key_found = True
            else:
                f.write(line)
        if not key_found:
            f.write(f"GEMINI_API_KEY={settings.api_key}\n")
            
    os.environ["GEMINI_API_KEY"] = settings.api_key
    # genai.configure(api_key=settings.api_key)
    return {"status": "success"}

@app.post("/api/equipment")
def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db)):
    db_equip = Equipment(**item.dict())
    db.add(db_equip)
    db.commit()
    db.refresh(db_equip)
    return db_equip

@app.put("/api/equipment/{equip_id}")
def update_equipment(equip_id: int, item: EquipmentCreate, db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if db_equip:
        for k, v in item.dict().items():
            setattr(db_equip, k, v)
        db.commit()
    return {"status": "success"}

@app.delete("/api/equipment/{equip_id}")
def delete_equipment(equip_id: int, db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if db_equip:
        db.delete(db_equip)
        db.commit()
    return {"status": "success"}

@app.post("/api/equipment/{equip_id}/photo")
async def upload_equipment_photo(equip_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if not db_equip:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    ext = file.filename.split(".")[-1]
    filename = f"eq_{equip_id}.{ext}"
    filepath = os.path.join("uploads", filename)
    with open(filepath, "wb") as buffer:
        buffer.write(await file.read())
        
    db_equip.photo_url = f"/uploads/{filename}"
    db.commit()
    return {"photo_url": db_equip.photo_url}

# -- CRM Companies --
@app.get("/api/companies")
def get_companies(db: Session = Depends(get_db)):
    return db.query(Company).all()

@app.post("/api/companies")
def create_company(comp: CompanyCreate, db: Session = Depends(get_db)):
    db_comp = Company(**comp.dict())
    db.add(db_comp)
    db.commit()
    db.refresh(db_comp)
    return db_comp

@app.get("/api/companies/{company_id}")
def get_company_detail(company_id: int, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    deals = db.query(Deal).filter(Deal.company_id == company_id).order_by(Deal.id.desc()).all()
    deals_data = []
    for d in deals:
        deals_data.append({
            "id": d.id,
            "title": d.title,
            "stage_name": d.stage_obj.name if d.stage_obj else "Unknown",
            "pipeline_name": d.pipeline.name if d.pipeline else "Unknown",
            "event_date": d.event_date,
            "final_sum": d.final_sum
        })

    return {
        "company": {
            "id": db_comp.id, "name": db_comp.name, "bin": db_comp.bin, "director_name": db_comp.director_name,
            "phone": db_comp.phone, "email": db_comp.email, "requisites": db_comp.requisites,
            "based_on": db_comp.based_on, "address": db_comp.address, "bank_name": db_comp.bank_name,
            "kbe": db_comp.kbe, "bik": db_comp.bik
        },
        "deals": deals_data
    }

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, comp: CompanyCreate, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    for key, value in comp.dict().items():
        setattr(db_comp, key, value)
        
    db.commit()
    db.refresh(db_comp)
    return db_comp

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    if db.query(Deal).filter(Deal.company_id == company_id).first():
        return JSONResponse(status_code=400, content={"error": "Cannot delete company with active deals"})
        
    db.delete(db_comp)
    db.commit()
    return {"status": "success"}

# -- WAHA (WhatsApp API) Proxies --
WAHA_URL = "http://127.0.0.1:3000"

@app.get("/api/wa/status")
def wa_status():
    try:
        r = requests.get(f"{WAHA_URL}/api/sessions/default", timeout=2)
        if r.status_code == 200:
            return r.json()
        return {"status": "NOT_FOUND"}
    except:
        return {"status": "OFFLINE", "error": "WAHA is not running"}

@app.post("/api/wa/start")
def wa_start():
    try:
        r = requests.post(f"{WAHA_URL}/api/sessions/start", json={"name": "default"}, timeout=5)
        return {"status": "started"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/wa/qr")
def wa_qr():
    try:
        # returns JSON {"mimetype": "image/png", "data": "base64..."} usually or we can request format
        r = requests.get(f"{WAHA_URL}/api/sessions/default/auth/qr?format=raw", timeout=2)
        if r.status_code == 200:
            from fastapi.responses import Response
            return Response(content=r.content, media_type="image/png")
        return JSONResponse(status_code=404, content={"error": "QR not ready or already scanned"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class SendMsg(BaseModel):
    phone: str
    text: str

@app.post("/api/wa/send")
def wa_send(msg: SendMsg):
    try:
        clean_phone = ''.join(filter(str.isdigit, msg.phone))
        if clean_phone.startswith('8'): clean_phone = '7' + clean_phone[1:]
        payload = {
            "session": "default",
            "chatId": f"{clean_phone}@c.us",
            "text": msg.text
        }
        r = requests.post(f"{WAHA_URL}/api/sendText", json=payload, timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# -- Custom Fields --
@app.get("/api/custom-fields")
def get_custom_fields(db: Session = Depends(get_db)):
    return db.query(CustomField).all()

@app.post("/api/custom-fields")
def create_custom_field(cf: CustomFieldCreate, db: Session = Depends(get_db)):
    db_cf = CustomField(name=cf.name, field_type=cf.field_type)
    db.add(db_cf)
    db.commit()
    db.refresh(db_cf)
    return db_cf

@app.get("/api/pipelines")
def get_pipelines(db: Session = Depends(get_db)):
    pipelines = db.query(Pipeline).all()
    return [{"id": p.id, "name": p.name} for p in pipelines]

@app.post("/api/pipelines")
def create_pipeline(pl: PipelineCreate, db: Session = Depends(get_db)):
    db_pl = Pipeline(name=pl.name)
    db.add(db_pl)
    db.commit()
    db.refresh(db_pl)
    return {"id": db_pl.id, "name": db_pl.name}

@app.delete("/api/pipelines/{pipeline_id}")
def delete_pipeline(pipeline_id: int, db: Session = Depends(get_db)):
    pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pl:
        return {"error": "Pipeline not found"}
    if db.query(Deal).filter(Deal.pipeline_id == pipeline_id).first():
        return {"error": "Cannot delete pipeline with active deals"}
    db.delete(pl)
    db.commit()
    return {"status": "ok"}

@app.get("/api/pipelines/{pipeline_id}/stages")
def get_stages(pipeline_id: int, db: Session = Depends(get_db)):
    stages = db.query(Stage).filter(Stage.pipeline_id == pipeline_id).order_by(Stage.order_index).all()
    return [{"id": s.id, "name": s.name, "order_index": s.order_index, "is_active_rent": s.is_active_rent} for s in stages]

@app.post("/api/pipelines/{pipeline_id}/stages")
def create_stage(pipeline_id: int, stage: StageCreate, db: Session = Depends(get_db)):
    st = Stage(pipeline_id=pipeline_id, name=stage.name, order_index=stage.order_index, is_active_rent=stage.is_active_rent)
    db.add(st)
    db.commit()
    db.refresh(st)
    return {"id": st.id, "name": st.name, "order_index": st.order_index, "is_active_rent": st.is_active_rent}

class StageUpdate(BaseModel):
    name: Optional[str] = None
    order_index: Optional[int] = None
    is_active_rent: Optional[bool] = None

@app.put("/api/stages/{stage_id}")
def update_stage(stage_id: int, stage_update: StageUpdate, db: Session = Depends(get_db)):
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return {"error": "Stage not found"}
    if stage_update.name is not None:
        st.name = stage_update.name
    if stage_update.order_index is not None:
        st.order_index = stage_update.order_index
    if stage_update.is_active_rent is not None:
        st.is_active_rent = stage_update.is_active_rent
    db.commit()
    return {"status": "ok"}

@app.delete("/api/stages/{stage_id}")
def delete_stage(stage_id: int, db: Session = Depends(get_db)):
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return {"error": "Stage not found"}
    if db.query(Deal).filter(Deal.stage == stage_id).first():
        return {"error": "Cannot delete stage with active deals"}
    db.delete(st)
    db.commit()
    return {"status": "ok"}

# -- CRM Deals --

from datetime import datetime

@app.get("/api/equipment/availability")
def get_equipment_availability(start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    if not start_date or not end_date:
        return []
    
    # Simple string-based comparison for now, assuming format YYYY-MM-DD
    # Or dd.mm.yyyy, need to make sure frontend sends ISO YYYY-MM-DD format
    # In a real app we would parse dates, but for this simple SQLite setup:
    # Let's just find deals that OVERLAP with the requested dates.
    # We will fetch all deals and filter in Python for safety since dates might be stored in different formats.
    
    all_deals = db.query(Deal).all()
    overlapping_deal_ids = []
    
    def parse_date(d_str):
        if not d_str: return None
        # Handle ISO strings or dates with time by taking the first 10 characters if it contains '-'
        try:
            if "T" in d_str: d_str = d_str.split("T")[0]
            elif " " in d_str: d_str = d_str.split(" ")[0]
            
            if "-" in d_str: return datetime.strptime(d_str, "%Y-%m-%d").date()
            if "." in d_str: return datetime.strptime(d_str, "%d.%m.%Y").date()
        except:
            pass
        return None

    s_dt = parse_date(start_date)
    e_dt = parse_date(end_date)
    
    if s_dt and e_dt:
        for deal in all_deals:
            ds_dt = parse_date(deal.setup_date) or parse_date(deal.event_date)
            de_dt = parse_date(deal.event_date) or parse_date(deal.setup_date)
            if not ds_dt or not de_dt:
                continue
                
            # Check overlap: Overlaps if (StartA <= EndB) and (EndA >= StartB)
            if s_dt <= de_dt and e_dt >= ds_dt:
                overlapping_deal_ids.append(deal.id)
                
    booked_items = {}
    if overlapping_deal_ids:
        # Fetch items belonging to overlapping deals
        items = db.query(DealItem).filter(DealItem.deal_id.in_(overlapping_deal_ids)).all()
        for item in items:
            deal_title = item.deal.title or f"Договор #{item.deal_id}"
            deal_dates = f"{item.deal.setup_date} - {item.deal.event_date}"
            if item.equipment_id not in booked_items:
                booked_items[item.equipment_id] = {"booked": 0, "conflicts": []}
            booked_items[item.equipment_id]["booked"] += item.quantity
            booked_items[item.equipment_id]["conflicts"].append({
                "deal_id": item.deal_id,
                "deal_title": deal_title,
                "dates": deal_dates,
                "qty": item.quantity
            })
            
    return [{"equipment_id": k, "booked": v["booked"], "conflicts": v["conflicts"]} for k, v in booked_items.items()]

@app.get("/api/deals")
def get_deals(pipeline_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Deal)
    if pipeline_id:
        query = query.filter(Deal.pipeline_id == pipeline_id)
    deals = query.all()
    # Serialize for JSON
    result = []
    for d in deals:
        comp_name = d.company.name if d.company else "Unknown"
        result.append({
            "id": d.id,
            "title": d.title,
            "company_name": comp_name,
            "pipeline_id": d.pipeline_id,
            "stage": d.stage,
            "event_date": d.event_date,
            "final_sum": d.final_sum
        })
    return result

@app.post("/api/deals")
def create_deal(deal: DealCreate, db: Session = Depends(get_db)):
    # Find first stage for the pipeline
    first_stage = db.query(Stage).filter(Stage.pipeline_id == deal.pipeline_id).order_by(Stage.order_index).first()
    stage_id = first_stage.id if first_stage else 1

    db_deal = Deal(
        title=deal.title,
        company_id=deal.company_id,
        pipeline_id=deal.pipeline_id,
        setup_date=deal.setup_date,
        event_date=deal.event_date,
        event_address=deal.event_address,
        discount_percentage=deal.discount_percentage,
        stage=stage_id
    )
    db.add(db_deal)
    db.commit()
    db.refresh(db_deal)
    
    history_entry = DealHistory(deal_id=db_deal.id, action_text="Сделка создана")
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

    return {"id": db_deal.id}

@app.put("/api/deals/{deal_id}/stage")
def update_deal_stage(deal_id: int, stage_update: DealStageUpdate, db: Session = Depends(get_db)):
    db_deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if db_deal:
        old_stage = db_deal.stage
        old_pipeline = db_deal.pipeline_id
        db_deal.stage = stage_update.stage
        if stage_update.pipeline_id is not None:
            db_deal.pipeline_id = stage_update.pipeline_id
        
        history_entry = DealHistory(deal_id=deal_id, action_text=f"Стадия изменена с {old_stage} на {stage_update.stage}")
        db.add(history_entry)
        db.commit()
        
        # Check if the new stage is "Монтаж / Мероприятие" (ID 5 by default) or name implies delivery
        new_stage_obj = db.query(Stage).filter(Stage.id == stage_update.stage).first()
        if new_stage_obj and ("Монтаж" in new_stage_obj.name or "доставлен" in new_stage_obj.name.lower()):
            company = db_deal.company
            if company:
                msg = f"Здравствуйте, {company.director_name or company.name}! Ваш заказ '{db_deal.title}' перешел в статус: {new_stage_obj.name}. Оборудование доставлено/монтируется."
                # 1. WhatsApp
                if company.phone:
                    notifications.send_wa_message(company.phone, msg)
                # 2. Telegram
                if company.telegram_chat_id:
                    notifications.send_tg_message(company.telegram_chat_id, msg)
                # 3. Web Push
                for sub in db_deal.push_subscriptions:
                    sub_info = {
                        "endpoint": sub.endpoint,
                        "keys": {
                            "p256dh": sub.p256dh,
                            "auth": sub.auth
                        }
                    }
                    notifications.send_web_push(sub_info, msg)
                    
    return {"status": "success"}

@app.get("/api/deals/{deal_id}")
def get_deal_detail(deal_id: int, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    items = []
    for i in d.items:
        items.append({
            "id": i.id,
            "equipment_id": i.equipment_id,
            "quantity": i.quantity,
            "days": i.days,
            "name": i.equipment.name if i.equipment else "Unknown",
            "price": i.equipment.price if i.equipment else 0,
            "category_type": "fixed" if i.equipment and i.equipment.category in ["Логистика", "Персонал", "Расходники"] else "equipment"
        })

    history = [{"action_text": h.action_text, "created_at": h.created_at.strftime("%Y-%m-%d %H:%M:%S")} for h in sorted(d.history, key=lambda x: x.created_at, reverse=True)]
    custom_values = {cv.field_id: cv.value for cv in d.custom_values}

    return {
        "id": d.id,
        "title": d.title,
        "company_id": d.company_id,
        "company_name": d.company.name if d.company else "Unknown",
        "company_phone": d.company.phone if d.company else "",
        "pipeline_id": d.pipeline_id,
        "stage": d.stage,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "discount_percentage": d.discount_percentage,
        "final_sum": d.final_sum,
        "items": items,
        "history": history,
        "custom_values": custom_values
    }

class DealItemsUpdate(BaseModel):
    discount_percentage: float
    items: List[dict] # {equipment_id, quantity, days}

@app.put("/api/deals/{deal_id}/items")
def update_deal_items(deal_id: int, update: DealItemsUpdate, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    # Remove old items
    db.query(DealItem).filter(DealItem.deal_id == deal_id).delete()
    
    # Add new items
    for i in update.items:
        di = DealItem(
            deal_id=deal_id,
            equipment_id=i["equipment_id"],
            quantity=i["quantity"],
            days=i["days"]
        )
        db.add(di)
        
    d.discount_percentage = update.discount_percentage
    db.commit()
    
    # Recalculate final sum
    calc_items = []
    for di in db.query(DealItem).filter(DealItem.deal_id == deal_id).all():
        eq = di.equipment
        cat_type = "fixed" if eq.category in ["Логистика", "Персонал", "Расходники"] else "equipment"
        calc_items.append({
            "name": eq.name,
            "price": eq.price,
            "quantity": di.quantity,
            "days": di.days,
            "category_type": cat_type
        })
        
    result = calculate_estimate(calc_items, update.discount_percentage)
    d.final_sum = result["grand_total"]
    db.commit()
    
    return {"status": "success", "final_sum": d.final_sum}

class DealUpdate(BaseModel):
    title: Optional[str] = None
    event_date: Optional[str] = None
    setup_date: Optional[str] = None
    event_address: Optional[str] = None
    comment: Optional[str] = None

@app.put("/api/deals/{deal_id}")
def update_deal(deal_id: int, update: DealUpdate, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    if update.title is not None:
        d.title = update.title
    if update.event_date is not None:
        d.event_date = update.event_date
    if update.setup_date is not None:
        d.setup_date = update.setup_date
    if update.event_address is not None:
        d.event_address = update.event_address
    if update.comment is not None:
        d.comment = update.comment
        
    db.commit()
    return {"status": "success"}

class DealCommentRequest(BaseModel):
    comment: str

@app.post("/api/deals/{deal_id}/comments")
def add_deal_comment(deal_id: int, request: DealCommentRequest, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    history_entry = DealHistory(
        deal_id=deal_id,
        action_text=f"Комментарий: {request.comment}"
    )
    db.add(history_entry)
    db.commit()
    return {"status": "success"}

@app.put("/api/deals/{deal_id}/fields")
def update_deal_fields(deal_id: int, update: DealFieldUpdateList, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    for field_update in update.fields:
        cv = db.query(DealFieldValue).filter(DealFieldValue.deal_id == deal_id, DealFieldValue.field_id == field_update.field_id).first()
        if cv:
            cv.value = field_update.value
        else:
            new_cv = DealFieldValue(deal_id=deal_id, field_id=field_update.field_id, value=field_update.value)
            db.add(new_cv)
            
    history_entry = DealHistory(deal_id=deal_id, action_text="Обновлены дополнительные поля")
    db.add(history_entry)
    db.commit()
    return {"status": "success"}

class PushSubModel(BaseModel):
    deal_id: int
    endpoint: str
    keys: dict

@app.post("/api/push/subscribe")
def subscribe_push(sub: PushSubModel, db: Session = Depends(get_db)):
    existing = db.query(PushSubscription).filter_by(endpoint=sub.endpoint, deal_id=sub.deal_id).first()
    if not existing:
        new_sub = PushSubscription(
            deal_id=sub.deal_id,
            endpoint=sub.endpoint,
            p256dh=sub.keys.get("p256dh", ""),
            auth=sub.keys.get("auth", "")
        )
        db.add(new_sub)
        db.commit()
    return {"status": "success"}

@app.get("/api/push/vapid_public_key")
def get_vapid_key():
    import os
    pub = os.getenv("VAPID_PUBLIC_KEY", "")
    return {"public_key": pub}

@app.post("/api/tg/webhook")
def tg_webhook(update: dict, db: Session = Depends(get_db)):
    if "message" in update and "text" in update["message"]:
        text = update["message"]["text"]
        chat_id = update["message"]["chat"]["id"]
        if text.startswith("/start company_"):
            try:
                company_id = int(text.split("_")[1])
                comp = db.query(Company).filter(Company.id == company_id).first()
                if comp:
                    comp.telegram_chat_id = str(chat_id)
                    db.commit()
                    import notifications
                    notifications.send_tg_message(str(chat_id), "Успешно! Теперь вы будете получать уведомления о заказах сюда.")
            except:
                pass
    return {"status": "ok"}

@app.get("/api/deals/{deal_id}/contract")
def download_deal_contract(deal_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    comp = d.company
    if not comp:
        return JSONResponse(status_code=400, content={"error": "No company linked"})

    calc_items = []
    for di in d.items:
        eq = di.equipment
        cat_type = "fixed" if eq.category in ["Логистика", "Персонал", "Расходники"] else "equipment"
        calc_items.append({
            "name": eq.name,
            "price": eq.price,
            "quantity": di.quantity,
            "days": di.days,
            "category_type": cat_type,
            "photo_url": eq.photo_url,
            "description": eq.description
        })
        
    result = calculate_estimate(calc_items, d.discount_percentage)
    
    context = {
        "contract_number": f"CRM-{d.id}",
        "contract_date": "Текущая дата",
        "company_name": comp.name,
        "director_name": comp.director_name,
        "iin_bin": comp.bin,
        "iban": comp.requisites,
        "event_name": d.title,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "discount_percentage": d.discount_percentage
    }
    
    template_path = os.path.join("templates", "contract_template.docx")
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    def cleanup_file(path: str):
        try: os.remove(path)
        except: pass
            
    background_tasks.add_task(cleanup_file, temp_path)
    
    return FileResponse(
        temp_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Contract_{d.id}_{comp.name}.docx"
    )

@app.get("/api/deals/{deal_id}/contract-preview")
async def get_deal_contract_preview(deal_id: int, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Deal not found")
        
    comp = db.query(Company).filter(Company.id == d.company_id).first()
    if not comp:
        raise HTTPException(status_code=400, detail="Deal has no associated company")
        
    items = []
    if d.items_json:
        try:
            items = json.loads(d.items_json)
        except Exception:
            pass
            
    result = calculate_estimate(items, d.discount_percentage)
    
    # Check equipment photos
    for item in result["items"]:
        eq = db.query(Equipment).filter(Equipment.name == item["name"]).first()
        if eq:
            item["photo_url"] = eq.photo_url
            item["description"] = eq.description
            
        item["price_text"] = f"₸{item['price']:,.2f}".replace(",", " ")
        item["subtotal_text"] = f"₸{item['line_total_discounted']:,.2f}".replace(",", " ")
            
    expanded_company = expand_company_type(comp.name)
    
    context = {
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        "company_name": expanded_company,
        "director_name": comp.director_name or "",
        "iin_bin": comp.bin or "",
        "iban": comp.requisites or "",
        "based_on": comp.based_on or "Устава",
        "company_address": comp.address or "",
        "bank_name": comp.bank_name or "",
        "kbe": comp.kbe or "",
        "bik": comp.bik or "",
        "event_name": d.event_name or "",
        "event_date": d.event_date or "",
        "event_address": d.event_address or "",
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "equipment_total_text": f"₸{result['equipment_total']:,.2f}".replace(",", " "),
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "grand_total_text": get_rubles_text(result["grand_total"]),
        "discount_percentage": d.discount_percentage
    }
    
    return JSONResponse(content=context)

# -- Calculator integration --
@app.post("/api/v1/calculate")
async def api_calculate(request: Request):
    data = await request.json()
    items = data.get("items", [])
    discount = float(data.get("discount", 0.0))
    result = calculate_estimate(items, discount)
    return JSONResponse(content=result)

@app.post("/api/preview-contract")
async def api_preview_contract(
    contract_number: str = Form(...),
    contract_date: str = Form(...),
    company_name: str = Form(...),
    director_name: str = Form(...),
    iin_bin: str = Form(...),
    iban: str = Form(...),
    based_on: str = Form("Устава"),
    company_address: str = Form(""),
    bank_name: str = Form(""),
    kbe: str = Form(""),
    bik: str = Form(""),
    event_name: str = Form(...),
    event_date: str = Form(...),
    event_address: str = Form(...),
    items_json: str = Form("[]"),
    discount: float = Form(0.0),
    db: Session = Depends(get_db)
):
    try:
        items = json.loads(items_json)
    except Exception:
        items = []

    calc_result = calculate_estimate(items, discount)
    for item in calc_result["items"]:
        eq = db.query(Equipment).filter(Equipment.name == item["name"]).first()
        if eq:
            item["photo_url"] = eq.photo_url
            item["description"] = eq.description
            
        item["price_text"] = f"₸{item['price']:,.2f}".replace(",", " ")
        item["subtotal_text"] = f"₸{item['line_total_discounted']:,.2f}".replace(",", " ")
            
    expanded_company = expand_company_type(company_name)
    
    context = {
        "contract_number": contract_number,
        "contract_date": contract_date,
        "company_name": expanded_company,
        "director_name": director_name,
        "iin_bin": iin_bin,
        "iban": iban,
        "based_on": based_on,
        "company_address": company_address,
        "bank_name": bank_name,
        "kbe": kbe,
        "bik": bik,
        "event_name": event_name,
        "event_date": event_date,
        "event_address": event_address,
        "items": calc_result["items"],
        "equipment_total": calc_result["equipment_total"],
        "equipment_total_text": f"₸{calc_result['equipment_total']:,.2f}".replace(",", " "),
        "fixed_total": calc_result["fixed_total"],
        "grand_total": calc_result["grand_total"],
        "grand_total_text": get_rubles_text(calc_result["grand_total"]),
        "discount_percentage": discount
    }
    
    return JSONResponse(content=context)

@app.post("/api/generate-html-preview")
async def api_generate_html_preview(request: Request):
    import io
    import mammoth
    from document_generator import generate_contract
    context = await request.json()
    
    template_path = os.path.join("templates", "contract_template.docx")
    
    # We can generate to a temp file, but doing it in memory is tricky with docxtpl unless we use temp file
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    with open(temp_path, "rb") as docx_file:
        result = mammoth.convert_to_html(docx_file)
        html_content = result.value
        
    try:
        os.remove(temp_path)
    except:
        pass
        
    return JSONResponse(content={"html": html_content})

@app.post("/api/download-contract")
async def api_download_contract(
    request: Request,
    background_tasks: BackgroundTasks
):
    context = await request.json()
    
    template_path = os.path.join("templates", "contract_template.docx")
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    def cleanup_file(path: str):
        try:
            os.remove(path)
        except Exception:
            pass
            
    background_tasks.add_task(cleanup_file, temp_path)
    
    filename = f"Contract_{context.get('contract_number', 'Doc')}.docx"
    return FileResponse(
        temp_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename
    )

@app.post("/api/v1/optimize-layout")
async def optimize_layout(data: LayoutOptimizationRequest):
    # Mock AI response
    response_data = {
        "status": "success",
        "message": "AI Layout Optimization calculated successfully.",
        "dimensions": {
            "width": data.width,
            "length": data.length
        },
        "placement_suggestions": [
            {
                "item_type": "sound_portal",
                "position": {"x": 0.5, "y": 0.5},
                "angle": 15,
                "note": "Оптимальный угол раскрытия для покрытия зала."
            },
            {
                "item_type": "truss",
                "position": {"x": data.width / 2, "y": data.length - 2},
                "angle": 0,
                "note": "Фермовая конструкция по центру."
            }
        ]
    }
    return JSONResponse(content=response_data)

@app.get("/api/deals/{deal_id}/2d-project")
def get_2d_project(deal_id: int, db: Session = Depends(get_db)):
    proj = db.query(Project2D).filter(Project2D.deal_id == deal_id).first()
    if proj:
        return {"layout_data_json": proj.layout_data_json}
    return {"layout_data_json": "[]"}

@app.post("/api/deals/{deal_id}/2d-project")
def save_2d_project(deal_id: int, proj: Project2DSave, db: Session = Depends(get_db)):
    p = db.query(Project2D).filter(Project2D.deal_id == deal_id).first()
    if p:
        p.layout_data_json = proj.layout_data_json
    else:
        new_p = Project2D(deal_id=deal_id, layout_data_json=proj.layout_data_json)
        db.add(new_p)
    db.commit()
    return {"status": "success"}

@app.post("/api/deals/{deal_id}/3d-scene")
async def analyze_3d_scene(
    deal_id: int, 
    file: UploadFile = File(...), 
    room_type: str = Form(...), 
    acoustics_level: str = Form(...),
    db: Session = Depends(get_db)
):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    equipment_summary = []
    for di in d.items:
        if di.equipment:
            equipment_summary.append(f"{di.quantity}x {di.equipment.name} ({di.equipment.category})")
            
    eq_text = ", ".join(equipment_summary) if equipment_summary else "оборудование не добавлено в смету"
            
    mock_response = {
        "status": "success",
        "file_analyzed": file.filename,
        "room_type": room_type,
        "acoustics_level": acoustics_level,
        "ai_analysis": f"Анализ помещения завершен. Обнаружен {room_type} тип площадки. Уровень акустики: {acoustics_level}. Для текущей сметы ({eq_text}) рекомендуется следующее размещение:",
        "recommendations": [
            {
                "item": "Звук",
                "action": "Разместите портальные системы под углом 15 градусов в сторону центра зала для минимизации переотражений от стен."
            },
            {
                "item": "Свет",
                "action": "Фермовые конструкции для света лучше установить в 5 метрах от края сцены для равномерного освещения."
            },
            {
                "item": "Экраны",
                "action": "Разместите LED экраны на высоте не менее 1.5м от уровня сцены для хорошего обзора с задних рядов."
            }
        ]
    }
    
    return JSONResponse(content=mock_response)

