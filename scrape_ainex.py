from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    routes = ['/', '/crm', '/inbox', '/quotes', '/tasks', '/analytics']
    
    for route in routes:
        print(f"Scraping {route}")
        page.goto(f"https://intro_showstudio.web.app{route}", wait_until='networkidle')
        html = page.content()
        name = 'index' if route == '/' else route.replace('/', '')
        with open(f"intro_show_{name}.html", 'w', encoding='utf-8') as f:
            f.write(html)
            
    browser.close()
