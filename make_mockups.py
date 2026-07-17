import os

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
        <div class="chat-list">
            <div class="chat-item active">
                <div class="chat-item-name">Айгерим Сапарова</div>
                <div class="chat-item-preview">Отлично! Подготовлю смету с крышей 8×8...</div>
            </div>
            <div class="chat-item">
                <div class="chat-item-name">Тимур Ахметов</div>
                <div class="chat-item-preview">Когда сможете прислать КП?</div>
            </div>
            <div class="chat-item">
                <div class="chat-item-name">Нурлан Оспанов</div>
                <div class="chat-item-preview">🎤 Голосовое сообщение</div>
            </div>
            <div class="chat-item">
                <div class="chat-item-name">ТОО "Event Solution"</div>
                <div class="chat-item-preview">Нам нужно звуковое оборудование на 500 человек.</div>
            </div>
        </div>
    </div>
    <div class="chat-main">
        <div class="chat-main-header">Айгерим Сапарова</div>
        <div class="chat-messages" id="messages-container">
            <div class="message received">Здравствуйте! Подскажите, свободна ли дата 21 мая для аренды звука и света?</div>
            <div class="message sent">Добрый день! Да, 21 мая свободно. Какое именно оборудование вас интересует?</div>
            <div class="message received">Нам нужно озвучить зал на 300 человек и поставить сценический свет.</div>
            <div class="message sent">Понял вас. Можем предложить комплект с линейными массивами и 8 световыми головами.</div>
            <div class="message received">Всё супер, согласовываем бюджет, вернусь сегодня</div>
            <div class="message sent">Отлично! Подготовлю смету с крышей 8×8, линиями JBL и световым комплектом</div>
        </div>
        <div class="chat-input-area">
            <input type="text" id="chat-input" class="chat-input" placeholder="Введите сообщение..." onkeypress="handleKeyPress(event)">
            <button class="send-btn" onclick="sendMessage()">Отправить</button>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
    function sendMessage() {
        const input = document.getElementById('chat-input');
        const text = input.value.trim();
        if (text) {
            const container = document.getElementById('messages-container');
            const msgDiv = document.createElement('div');
            msgDiv.className = 'message sent';
            msgDiv.textContent = text;
            container.appendChild(msgDiv);
            input.value = '';
            container.scrollTop = container.scrollHeight;
        }
    }
    
    function handleKeyPress(e) {
        if (e.key === 'Enter') {
            sendMessage();
        }
    }
</script>
{% endblock %}
"""

assistant_html = """{% extends "base.html" %}

{% block title %}AI Чат-бот — Intro Show{% endblock %}

{% block extra_head %}
<style>
    .main {
        padding: 0 !important;
        max-width: none !important;
    }
    .assistant-container {
        display: flex;
        flex-direction: column;
        height: calc(100vh - 69px);
        background-color: var(--background);
        color: var(--text);
        max-width: 900px;
        margin: 0 auto;
        width: 100%;
        border-left: 1px solid var(--border);
        border-right: 1px solid var(--border);
    }
    .assistant-header {
        padding: 20px;
        border-bottom: 1px solid var(--border);
        background-color: var(--surface);
        display: flex;
        align-items: center;
        gap: 15px;
    }
    .bot-icon {
        width: 40px;
        height: 40px;
        border-radius: 8px;
        background: linear-gradient(135deg, var(--primary), var(--accent));
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .bot-info h2 {
        margin: 0;
        font-size: 18px;
    }
    .bot-info p {
        margin: 0;
        font-size: 13px;
        color: var(--text-muted, #888);
    }
    .assistant-messages {
        flex: 1;
        padding: 20px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 15px;
    }
    .msg-row {
        display: flex;
        gap: 15px;
        max-width: 85%;
    }
    .msg-row.ai {
        align-self: flex-start;
    }
    .msg-row.user {
        align-self: flex-end;
        flex-direction: row-reverse;
    }
    .msg-bubble {
        padding: 15px;
        border-radius: 12px;
        font-size: 15px;
        line-height: 1.5;
    }
    .msg-row.ai .msg-bubble {
        background-color: var(--surface);
        border: 1px solid var(--border);
    }
    .msg-row.user .msg-bubble {
        background-color: var(--primary);
        color: white;
    }
    .assistant-input-area {
        padding: 20px;
        background-color: var(--surface);
        border-top: 1px solid var(--border);
    }
    .input-wrapper {
        display: flex;
        gap: 10px;
        background-color: var(--background);
        border: 1px solid var(--border);
        border-radius: 24px;
        padding: 8px;
    }
    .assistant-input {
        flex: 1;
        padding: 10px 15px;
        border: none;
        background: transparent;
        color: var(--text);
        outline: none;
        font-size: 15px;
    }
    .send-btn {
        background-color: var(--primary);
        color: white;
        border: none;
        width: 40px;
        height: 40px;
        border-radius: 50%;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .send-btn:hover {
        opacity: 0.9;
    }
</style>
{% endblock %}

{% block content %}
<div class="assistant-container">
    <div class="assistant-header">
        <div class="bot-icon">
            <svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" viewBox="0 0 24 24" width="24" height="24"><path d="M12 2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2 2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM4 10h16v12H4zM8 14v4M16 14v4"></path></svg>
        </div>
        <div class="bot-info">
            <h2>Intro Show Copilot</h2>
            <p>Ваш персональный ИИ-ассистент по аренде оборудования</p>
        </div>
    </div>
    
    <div class="assistant-messages" id="bot-messages-container">
        <div class="msg-row ai">
            <div class="msg-bubble">
                Привет! Я ИИ-ассистент Intro Show. Я могу помочь вам быстро составить смету, подобрать звуковое и световое оборудование для мероприятия, или проанализировать данные из CRM. Чем могу помочь сегодня?
            </div>
        </div>
    </div>
    
    <div class="assistant-input-area">
        <div class="input-wrapper">
            <input type="text" id="assistant-input" class="assistant-input" placeholder="Напишите ваш запрос (например: Подбери звук на 500 человек)..." onkeypress="handleBotKeyPress(event)">
            <button class="send-btn" onclick="sendBotMessage()">
                <svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" viewBox="0 0 24 24" width="20" height="20"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
            </button>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
    function sendBotMessage() {
        const input = document.getElementById('assistant-input');
        const text = input.value.trim();
        if (text) {
            const container = document.getElementById('bot-messages-container');
            
            // User message
            const userRow = document.createElement('div');
            userRow.className = 'msg-row user';
            userRow.innerHTML = `<div class="msg-bubble">${text}</div>`;
            container.appendChild(userRow);
            
            input.value = '';
            container.scrollTop = container.scrollHeight;
            
            // Bot typing simulation
            setTimeout(() => {
                const aiRow = document.createElement('div');
                aiRow.className = 'msg-row ai';
                aiRow.innerHTML = `<div class="msg-bubble">Спасибо за запрос! В данный момент я работаю в демо-режиме, но скоро я смогу анализировать площадки и подбирать оборудование автоматически. 😉</div>`;
                container.appendChild(aiRow);
                container.scrollTop = container.scrollHeight;
            }, 1000);
        }
    }
    
    function handleBotKeyPress(e) {
        if (e.key === 'Enter') {
            sendBotMessage();
        }
    }
</script>
{% endblock %}
"""

with open('/Users/maximbilyalov/Documents/КОС/rental_app/templates/inbox.html', 'w') as f:
    f.write(inbox_html)

with open('/Users/maximbilyalov/Documents/КОС/rental_app/templates/assistant.html', 'w') as f:
    f.write(assistant_html)

print("Mockups created.")
