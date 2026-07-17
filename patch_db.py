import os

with open("database.py", "r") as f:
    content = f.read()

replacement = """import os
import shutil

# Vercel filesystem is read-only except for /tmp
if os.environ.get("VERCEL"):
    db_path = "/tmp/rental_app.db"
    if not os.path.exists(db_path):
        # Copy the bundled database to /tmp so it's writable
        original_db = os.path.join(os.path.dirname(__file__), "rental_app.db")
        if os.path.exists(original_db):
            shutil.copy(original_db, db_path)
    DATABASE_URL = f"sqlite:///{db_path}"
else:
    DATABASE_URL = "sqlite:////Users/maximbilyalov/Documents/КОС/rental_app/rental_app.db"
"""

content = content.replace('DATABASE_URL = "sqlite:////Users/maximbilyalov/Documents/КОС/rental_app/rental_app.db"', replacement)

with open("database.py", "w") as f:
    f.write(content)

print("database.py patched for Vercel")
