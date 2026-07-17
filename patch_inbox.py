import re

inbox_html = """{% extends "base.html" %}

{% block title %}WhatsApp Inbox - Intro Show{% endblock %}

{% block extra_head %}
<style>
    .main {
        padding: 0 !important;
        max-width: none !important;
    }
    .chat-container {
        display: flex;
        height: calc(100vh - 69px);
        background-color: var(--background);
        color: var(--text);
    }
    .chat-sidebar {
        width: 300px;
        background-color: var(--surface);
        border-right: 1px solid var(--border);
        display: flex;
        flex-direction: column;
    }
    .chat-header {
        padding: 15px;
        border-bottom: 1px solid var(--border);
        font-weight: bold;
        background-color: var(--surface);
        font-size: 16px;
    }
    .chat-list {
        overflow-y: auto;
        flex: 1;
    }
    .chat-item {
        padding: 15px;
        border-bottom: 1px solid var(--border);
        cursor: pointer;
        display: flex;
        flex-direction: column;
    }
    .chat-item:hover {
        background-color: var(--background);
    }
    .chat-item.active {
        background-color: var(--background);
        border-left: 4px solid var(--primary);
    }
    .chat-item-name {
        font-weight: bold;
        margin-bottom: 5px;
    }
    .chat-item-preview {
        font-size: 12px;
        color: var(--text-muted, #888);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .chat-main {
        flex: 1;
        display: flex;
        flex-direction: column;
        background-color: var(--background);
    }
    .chat-main-header {
        padding: 15px;
        background-color: var(--surface);
        border-bottom: 1px solid var(--border);
        font-weight: bold;
        font-size: 16px;
    }
    .chat-messages {
        flex: 1;
        padding: 20px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 10px;
    }
    .message {
        max-width: 60%;
        padding: 10px 15px;
        border-radius: 8px;
        font-size: 14px;
        line-height: 1.4;
    }
    .message.received {
        align-self: flex-start;
        background-color: var(--surface);
        border: 1px solid var(--border);
    }
    .message.sent {
        align-self: flex-end;
        background-color: var(--primary);
        color: white;
    }
    .chat-input-area {
        padding: 15px;
        background-color: var(--surface);
        border-top: 1px solid var(--border);
        display: flex;
        gap: 10px;
    }
    .chat-input {
        flex: 1;
        padding: 10px 15px;
        border-radius: 20px;
        border: 1px solid var(--border);
        background-color: var(--background);
        color: var(--text);
        outline: none;
    }
    .send-btn {
        background-color: var(--primary);
        color: white;
        border: none;
        padding: 10px 20px;
        border-radius: 20px;
        cursor: pointer;
        font-weight: bold;
    }
    .send-btn:hover {
        opacity: 0.9;
    }
</style>
{% endblock %}

{% block content %}
<div class="chat-container">
    <div class="chat-sidebar">
        <div class="chat-header">WhatsApp Диалоги</div>
        <div class="chat-list" id="chat-list">
            <!-- Rendered by JS -->
        </div>
    </div>
    <div class="chat-main">
        <div class="chat-main-header" id="chat-title">Выберите чат</div>
        <div class="chat-messages" id="messages-container">
            <!-- Rendered by JS -->
        </div>
        <div class="chat-input-area" id="chat-input-area" style="display: none;">
            <input type="text" id="chat-input" class="chat-input" placeholder="Введите сообщение..." onkeypress="handleKeyPress(event)">
            <button class="send-btn" onclick="sendMessage()">Отправить</button>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
    const chatsData = {
        'aigerim': {
            name: 'Айгерим Сапарова',
            preview: 'Отлично! Подготовлю смету с крышей 8×8...',
            messages: [
                { type: 'received', text: 'Здравствуйте! Подскажите, свободна ли дата 21 мая для аренды звука и света?' },
                { type: 'sent', text: 'Добрый день! Да, 21 мая свободно. Какое именно оборудование вас интересует?' },
                { type: 'received', text: 'Нам нужно озвучить зал на 300 человек и поставить сценический свет.' },
                { type: 'sent', text: 'Понял вас. Можем предложить комплект с линейными массивами и 8 световыми головами.' },
                { type: 'received', text: 'Всё супер, согласовываем бюджет, вернусь сегодня' },
                { type: 'sent', text: 'Отлично! Подготовлю смету с крышей 8×8, линиями JBL и световым комплектом' }
            ]
        },
        'timur': {
            name: 'Тимур Ахметов',
            preview: 'Когда сможете прислать КП?',
            messages: [
                { type: 'received', text: 'Привет! Мы делаем корпоратив на 150 человек.' },
                { type: 'sent', text: 'Привет! Отлично, какая площадка?' },
                { type: 'received', text: 'Ресторан Almaty Hall. Нам нужен базовый звук и проектор.' },
                { type: 'sent', text: 'Без проблем, пришлю вам варианты.' },
                { type: 'received', text: 'Когда сможете прислать КП?' }
            ]
        },
        'nurlan': {
            name: 'Нурлан Оспанов',
            preview: '🎤 Голосовое сообщение',
            messages: [
                { type: 'received', text: 'Салам! Есть в аренду Pioneer CDJ-3000?' },
                { type: 'sent', text: 'Привет! Да, есть. На какую дату?' },
                { type: 'received', text: 'На эту субботу. Сможете привезти на Медеу?' },
                { type: 'sent', text: 'Да, доставка будет стоить 5000 тг.' },
                { type: 'received', text: '🎤 [Голосовое сообщение 0:14]' }
            ]
        },
        'eventsolution': {
            name: 'ТОО "Event Solution"',
            preview: 'Нам нужно звуковое оборудование на 500 человек.',
            messages: [
                { type: 'received', text: 'Добрый день. Это агентство Event Solution.' },
                { type: 'sent', text: 'Здравствуйте! Слушаем вас.' },
                { type: 'received', text: 'Нам нужно звуковое оборудование на 500 человек. Конференция в отеле Rixos.' }
            ]
        }
    };

    let activeChatId = null;

    function renderSidebar() {
        const list = document.getElementById('chat-list');
        list.innerHTML = '';
        for (const [id, chat] of Object.entries(chatsData)) {
            const div = document.createElement('div');
            div.className = `chat-item ${id === activeChatId ? 'active' : ''}`;
            div.onclick = () => selectChat(id);
            div.innerHTML = `
                <div class="chat-item-name">${chat.name}</div>
                <div class="chat-item-preview">${chat.preview}</div>
            `;
            list.appendChild(div);
        }
    }

    function selectChat(id) {
        activeChatId = id;
        renderSidebar();
        
        const chat = chatsData[id];
        document.getElementById('chat-title').innerText = chat.name;
        document.getElementById('chat-input-area').style.display = 'flex';
        
        const container = document.getElementById('messages-container');
        container.innerHTML = '';
        chat.messages.forEach(msg => {
            const msgDiv = document.createElement('div');
            msgDiv.className = `message ${msg.type}`;
            msgDiv.textContent = msg.text;
            container.appendChild(msgDiv);
        });
        container.scrollTop = container.scrollHeight;
    }

    function sendMessage() {
        if (!activeChatId) return;
        const input = document.getElementById('chat-input');
        const text = input.value.trim();
        if (text) {
            chatsData[activeChatId].messages.push({ type: 'sent', text: text });
            chatsData[activeChatId].preview = text;
            
            const container = document.getElementById('messages-container');
            const msgDiv = document.createElement('div');
            msgDiv.className = 'message sent';
            msgDiv.textContent = text;
            container.appendChild(msgDiv);
            
            input.value = '';
            container.scrollTop = container.scrollHeight;
            renderSidebar();
        }
    }
    
    function handleKeyPress(e) {
        if (e.key === 'Enter') {
            sendMessage();
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const urlParams = new URLSearchParams(window.location.search);
        const requestedChat = urlParams.get('chat');
        if (requestedChat && chatsData[requestedChat]) {
            selectChat(requestedChat);
        } else {
            selectChat('aigerim'); // default
        }
    });
</script>
{% endblock %}
"""

with open('/Users/maximbilyalov/Documents/КОС/rental_app/templates/inbox.html', 'w') as f:
    f.write(inbox_html)

crm_path = '/Users/maximbilyalov/Documents/КОС/rental_app/templates/crm.html'
with open(crm_path, 'r') as f:
    crm_content = f.read()

crm_content = re.sub(
    r'id="deal-chat-btn" class="slider-action-icon" title="Написать в WhatsApp/Telegram" href="#"',
    r'id="deal-chat-btn" class="slider-action-icon" title="Написать в WhatsApp/Telegram" href="/inbox?chat=aigerim"',
    crm_content
)

# Wait, the deal-chat-btn href in crm is static? Let's make it dynamic or just link to `/inbox`.
# If I just link to `/inbox`, it works. If I want it dynamic, it might be updated in JS.
# Let's just update the href.
with open(crm_path, 'w') as f:
    f.write(crm_content)

print("Mockups updated.")
