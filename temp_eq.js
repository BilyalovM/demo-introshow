        let equipmentData = [];
        let foldersData = [];
        let currentFolderId = null;

        async function loadData() {
            try {
                const [eqRes, fRes] = await Promise.all([
                    fetch('/api/equipment?t=' + new Date().getTime()), 
                    fetch('/api/folders?t=' + new Date().getTime())
                ]);
                if (!eqRes.ok || !fRes.ok) {
                    alert('Ошибка сети: ' + eqRes.status);
                    return;
                }
                equipmentData = await eqRes.json();
                foldersData = await fRes.json();
                renderView();
                populateFolderSelect();
            } catch (err) { 
                alert("Ошибка загрузки данных: " + err.message);
                console.error("Failed to load data", err); 
            }
        }

        function populateFolderSelect() {
            const select = document.getElementById('eq_folder_id');
            select.innerHTML = '<option value="">-- Корень --</option>';
            foldersData.forEach(f => select.innerHTML += `<option value="${f.id}">${f.name}</option>`);
        }

        function renderBreadcrumbs() {
            const bc = document.getElementById('breadcrumbs');
            bc.innerHTML = `<span class="breadcrumb-item" onclick="navigateTo(null)">Склад</span>`;
            if (currentFolderId) {
                const path = [];
                let curr = foldersData.find(f => f.id === currentFolderId);
                while (curr) { path.unshift(curr); curr = foldersData.find(f => f.id === curr.parent_id); }
                path.forEach(f => bc.innerHTML += `<span class="breadcrumb-separator">/</span><span class="breadcrumb-item" onclick="navigateTo(${f.id})">${f.name}</span>`);
            }
        }

        function navigateTo(folderId) { currentFolderId = folderId; renderView(); }

        function renderView() {
            renderBreadcrumbs();
            const search = document.getElementById('searchInput').value.toLowerCase();
            const grid = document.getElementById('foldersGrid');
            grid.innerHTML = '';
            foldersData.filter(f => search ? f.name.toLowerCase().includes(search) : f.parent_id === currentFolderId).forEach(f => {
                grid.innerHTML += `<div class="folder-card" onclick="navigateTo(${f.id})"><div class="folder-icon">📁</div><div class="folder-name">${f.name}</div></div>`;
            });

            const tbody = document.querySelector('#equipTable tbody');
            tbody.innerHTML = '';
            equipmentData.filter(e => search ? e.name.toLowerCase().includes(search) : e.folder_id === currentFolderId).forEach(item => {
                const tr = document.createElement('tr');
                let img = item.photo_url ? `<img src="${item.photo_url}" style="width:40px; height:40px; object-fit:cover; border-radius:4px;">` : '📷';
                let statusBadge = `<span style="color: ${item.status === 'Доступно' ? 'green' : 'red'}">${item.status}</span>`;
                let customFieldsHtml = '';
                if (item.custom_fields && Object.keys(item.custom_fields).length > 0) {
                    customFieldsHtml = '<div style="margin-top: 0.5rem; font-size: 0.85rem; color: var(--text-light);">';
                    for (const [k, v] of Object.entries(item.custom_fields)) customFieldsHtml += `<span style="display:inline-block; background: var(--bg); padding: 0.2rem 0.5rem; border-radius: 4px; margin-right: 0.5rem; margin-bottom: 0.2rem;"><b>${k}:</b> ${v}</span>`;
                    customFieldsHtml += '</div>';
                }
                let availColor = item.available_quantity <= 0 ? 'color: var(--danger); font-weight: bold;' : '';
                tr.innerHTML = `<td>${item.id}</td><td>${img}</td><td><strong>${item.name}</strong><br><small style="color:var(--text-2)">Категория: ${item.category}</small></td><td>${item.price.toLocaleString()} ₸</td><td>${item.stock_quantity}</td><td>${item.rented_quantity}</td><td style="${availColor}">${item.available_quantity}</td><td>${statusBadge}</td><td><div style="font-size: 0.85rem;">${item.description || ''}</div>${customFieldsHtml}</td><td class="actions"><div style="display:flex;gap:0.5rem;"><button class="btn btn-sm" onclick='openEquipModal(${JSON.stringify(item).replace(/'/g, "&apos;")})'>Редакт.</button> <button class="btn btn-sm btn-danger" style="background:var(--danger);color:white;border:none;" onclick="deleteEquipment(${item.id})">Удалить</button></div></td>`;
                tbody.appendChild(tr);
            });
        }

        function openFolderModal() {
            document.getElementById('f_name').value = '';
            document.getElementById('folderModal').style.display = 'flex';
        }

        async function saveFolder(e) {
            e.preventDefault();
            const name = document.getElementById('f_name').value;
            try {
                await fetch('/api/folders', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, parent_id: currentFolderId })
                });
                closeModal('folderModal');
                loadData();
            } catch(e) { alert("Ошибка при создании папки"); }
        }

        function closeModal(id) {
            document.getElementById(id).style.display = 'none';
        }

        async function uploadPhoto() {
            const id = document.getElementById('eq_id').value;
            const fileInput = document.getElementById('eq_photo_input');
            if(!id || fileInput.files.length === 0) return;
            
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            
            document.getElementById('upload_status').style.display = 'block';
            try {
                const res = await fetch(`/api/equipment/${id}/photo`, {
                    method: 'POST', body: formData
                });
                const data = await res.json();
                
                document.getElementById('eq_photo_preview').innerHTML = `<img src="${data.photo_url}">`;
                
                const item = equipmentData.find(e => e.id == id);
                if(item) item.photo_url = data.photo_url;
                renderView();
            } catch(e) {
                alert("Ошибка загрузки фото");
            } finally {
                document.getElementById('upload_status').style.display = 'none';
            }
        }

        function addCustomField(key = '', value = '') {
            const container = document.getElementById('customFieldsContainer');
            const row = document.createElement('div');
            row.style.display = 'flex'; row.style.gap = '0.5rem';
            row.innerHTML = `<input type="text" placeholder="Поле" class="cf-key" value="${key}" style="flex: 1;"><input type="text" placeholder="Значение" class="cf-val" value="${value}" style="flex: 1;"><button type="button" onclick="this.parentElement.remove()" class="btn-danger" style="padding: 0.5rem;">✕</button>`;
            container.appendChild(row);
        }

        function openEquipModal(item = null) {
            document.getElementById('equipForm').reset();
            document.getElementById('customFieldsContainer').innerHTML = '';
            const photoPreview = document.getElementById('eq_photo_preview');
            if (item) {
                document.getElementById('eq_modal_title').innerText = "Редактировать оборудование";
                document.getElementById('eq_id').value = item.id;
                document.getElementById('eq_name').value = item.name;
                document.getElementById('eq_category').value = item.category;
                document.getElementById('eq_folder_id').value = item.folder_id || '';
                document.getElementById('eq_price').value = item.price;
                document.getElementById('eq_qty').value = item.stock_quantity;
                document.getElementById('eq_status').value = item.status;
                document.getElementById('eq_desc').value = item.description || '';
                if (item.custom_fields) for (const [k, v] of Object.entries(item.custom_fields)) addCustomField(k, v);
                photoPreview.innerHTML = item.photo_url ? `<img src="${item.photo_url}">` : '<span style="color:var(--text-light)">Нет фото</span>';
                document.getElementById('eq_photo_input').style.display = 'block';
            } else {
                document.getElementById('eq_modal_title').innerText = "Добавить оборудование";
                document.getElementById('eq_id').value = '';
                document.getElementById('eq_folder_id').value = currentFolderId || '';
                photoPreview.innerHTML = `<span style="color:var(--text-light)">Сохраните позицию перед загрузкой фото</span>`;
                document.getElementById('eq_photo_input').style.display = 'none';
            }
            document.getElementById('equipModal').style.display = 'flex';
        }

        async function saveEquipment(e) {
            e.preventDefault();
            const id = document.getElementById('eq_id').value;
            const custom_fields = {};
            document.querySelectorAll('.cf-key').forEach((kInput, idx) => {
                const k = kInput.value.trim();
                const v = document.querySelectorAll('.cf-val')[idx].value.trim();
                if (k) custom_fields[k] = v;
            });
            const data = {
                name: document.getElementById('eq_name').value,
                category: document.getElementById('eq_category').value,
                price: parseFloat(document.getElementById('eq_price').value),
                stock_quantity: parseInt(document.getElementById('eq_qty').value),
                status: document.getElementById('eq_status').value,
                folder_id: document.getElementById('eq_folder_id').value || null,
                description: document.getElementById('eq_desc').value,
                custom_fields
            };

            try {
                if (id) {
                    await fetch(`/api/equipment/${id}`, {
                        method: 'PUT', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                } else {
                    await fetch('/api/equipment', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                }
                closeModal('equipModal');
                loadData();
            } catch (err) {
                console.error("Error saving", err);
            }
        }

        async function deleteEquipment(id) {
            if(!confirm("Удалить позицию?")) return;
            try {
                await fetch(`/api/equipment/${id}`, { method: 'DELETE' });
                loadData();
            } catch (err) {
                console.error("Error deleting", err);
            }
        }

        function addCustomField(key = '', value = '') {
            const container = document.getElementById('customFieldsContainer');
            const row = document.createElement('div');
            row.style.display = 'flex';
            row.style.gap = '0.5rem';
            row.innerHTML = `
                <input type="text" placeholder="Поле (напр. Цвет)" class="cf-key" value="${key}" style="flex: 1;">
                <input type="text" placeholder="Значение" class="cf-val" value="${value}" style="flex: 1;">
                <button type="button" onclick="this.parentElement.remove()" style="padding: 0.5rem; background: var(--danger); color: white; border-radius: 4px; border: none; cursor: pointer;">✕</button>
            `;
            container.appendChild(row);
        }

        document.addEventListener('DOMContentLoaded', loadData);
