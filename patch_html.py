import re

base_path = '/Users/maximbilyalov/Documents/КОС/rental_app/templates/base.html'
with open(base_path, 'r') as f:
    content = f.read()

# Replace first logo (desktop sidebar)
content = re.sub(
    r'<div class="brand-logo">\s*<svg.*?>.*?</svg>\s*</div>',
    '<div class="brand-logo" style="background: transparent;"><img src="{{ url_for(\'static\', path=\'img/logo.jpg\') }}" alt="Intro Show" style="height: 32px; width: auto; border-radius: 4px;" /></div>',
    content,
    flags=re.DOTALL
)

# Replace second logo (mobile topbar)
content = re.sub(
    r'<span class="brand-logo"[^>]*>\s*<svg.*?>.*?</svg>\s*</span>',
    '<span class="brand-logo" style="background: transparent; display: grid; place-items: center; height: 100%;"><img src="{{ url_for(\'static\', path=\'img/logo.jpg\') }}" alt="Intro Show" style="height: 24px; width: auto;" /></span>',
    content,
    flags=re.DOTALL
)

with open(base_path, 'w') as f:
    f.write(content)


settings_path = '/Users/maximbilyalov/Documents/КОС/rental_app/templates/settings.html'
with open(settings_path, 'r') as f:
    content = f.read()

company_info = """
        <div class="card">
            <h3 style="margin-top:0;">Информация о компании (Intro Show)</h3>
            <div style="margin-top: 1rem; line-height: 1.6; color: var(--text);">
                <p><strong>Адрес:</strong> Тюлькубасская улица, 4, Алматы, Алматинская область, Казахстан</p>
                <p><strong>График работы:</strong> Пн - Вс, 10:00 - 22:00</p>
                <p><strong>Телефоны:</strong></p>
                <ul style="margin-left: 1.5rem; margin-bottom: 1rem;">
                    <li>+7 (701) 554-13-80 (менеджер)</li>
                    <li>+7 (702) 227-66-73 (менеджер)</li>
                </ul>
                <p><strong>Email:</strong> show.intro@yandex.kz</p>
                <p><strong>Сайт:</strong> <a href="https://intro-show-rental.satu.kz/" target="_blank" style="color: var(--primary);">intro-show-rental.satu.kz</a></p>
            </div>
        </div>
"""

# Insert company info before the first card
content = content.replace('<h2>Настройки системы</h2>', '<h2>Настройки системы</h2>\n' + company_info)

with open(settings_path, 'w') as f:
    f.write(content)

print("HTML patched successfully.")
