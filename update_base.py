with open('templates/base.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Replace the companies nav link
html = html.replace(''' <a href="{{ url_for('read_companies') }}" class="nav-link {% if request.url.path == '/companies' %}active{% endif %}">
  <span class="nav-ico">
   <svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" viewbox="0 0 24 24">
    <path d="M3 3v18h18">
    </path>
    <path d="M7 14l3-3 3 3 5-6">
    </path>
   </svg>
  </span>
  <span>
   Клиенты
  </span>
 </a>''', ''' <a href="/tasks" class="nav-link {% if request.url.path == '/tasks' %}active{% endif %}">
  <span class="nav-ico">
   <svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" viewbox="0 0 24 24">
    <path d="M9 11l3 3L22 4"></path>
    <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>
   </svg>
  </span>
  <span>
   Задачи
  </span>
 </a>
 <a href="/analytics" class="nav-link {% if request.url.path == '/analytics' %}active{% endif %}">
  <span class="nav-ico">
   <svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" viewbox="0 0 24 24">
    <path d="M3 3v18h18"></path>
    <path d="M7 14l3-3 3 3 5-6"></path>
   </svg>
  </span>
  <span>
   Аналитика
  </span>
 </a>''')

# Update inbox active state
html = html.replace('<a href="/inbox" class="nav-link">', '<a href="/inbox" class="nav-link {% if request.url.path == \'/inbox\' %}active{% endif %}">')

with open('templates/base.html', 'w', encoding='utf-8') as f:
    f.write(html)
    
print("Updated base.html")
