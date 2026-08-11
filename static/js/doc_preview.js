/**
 * In-CRM document preview drawer: contenteditable HTML → overrides → PDF/Word.
 * Shared by /documents, /quotes/, CRM deal card.
 */
(function (global) {
  'use strict';

  let _previewDocType = null;
  let _previewDealId = null;
  let _previewMode = 'html';
  let _previewBlobUrl = null;
  let _sessionOverrides = {
    custom_title: '',
    body_notes: '',
    footer_notes: '',
    context_overrides: {},
    items: null,
  };
  let _blurTimer = null;
  let _refreshing = false;

  function $(id) {
    return document.getElementById(id);
  }

  function showToast(msg) {
    const el = $('pv-toast');
    if (!el) {
      console.log(msg);
      return;
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2200);
  }

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function revokePreviewBlob() {
    if (_previewBlobUrl) {
      try {
        URL.revokeObjectURL(_previewBlobUrl);
      } catch (_) {}
      _previewBlobUrl = null;
    }
  }

  function parseMoney(s) {
    const t = String(s || '')
      .replace(/\u00a0/g, ' ')
      .replace(/\s/g, '')
      .replace(',', '.')
      .replace(/[^\d.-]/g, '');
    const n = parseFloat(t);
    return Number.isFinite(n) ? n : 0;
  }

  function collectFromFrame() {
    const frame = $('pv-frame');
    const doc = frame && (frame.contentDocument || frame.contentWindow?.document);
    if (!doc) return null;

    const context_overrides = {};
    const itemsMap = {};
    let custom_title = null;
    let body_notes = null;
    let footer_notes = null;

    doc.querySelectorAll('[data-field][contenteditable="true"]').forEach((el) => {
      const field = el.getAttribute('data-field');
      if (!field) return;
      const isBlock = el.classList.contains('pv-ed-block') || field === 'body_notes' || field === 'footer_notes';
      let val = isBlock
        ? (el.innerText || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trimEnd()
        : (el.innerText || '').replace(/\s+/g, ' ').trim();

      if (field === 'custom_title') {
        custom_title = val;
        return;
      }
      if (field === 'body_notes') {
        body_notes = val;
        return;
      }
      if (field === 'footer_notes') {
        footer_notes = val;
        return;
      }
      const m = field.match(/^items\.(\d+)\.(\w+)$/);
      if (m) {
        const idx = parseInt(m[1], 10);
        const key = m[2];
        if (!itemsMap[idx]) itemsMap[idx] = {};
        if (key === 'quantity' || key === 'qty' || key === 'days' || key === 'price' || key === 'line_sum') {
          itemsMap[idx][key === 'qty' ? 'quantity' : key] = parseMoney(val);
        } else {
          itemsMap[idx][key] = val;
        }
        return;
      }
      context_overrides[field] = val;
    });

    const maxIdx = Math.max(-1, ...Object.keys(itemsMap).map((k) => parseInt(k, 10)));
    const items = maxIdx >= 0 ? [] : null;
    if (items) {
      for (let i = 0; i <= maxIdx; i++) {
        items.push(itemsMap[i] || {});
      }
    }

    return { custom_title, body_notes, footer_notes, context_overrides, items };
  }

  function localRecalcLineSums(doc) {
    if (!doc) return;
    doc.querySelectorAll('tr[data-item-index]').forEach((tr) => {
      const idx = tr.getAttribute('data-item-index');
      const qtyEl = tr.querySelector(`[data-field="items.${idx}.quantity"]`);
      const daysEl = tr.querySelector(`[data-field="items.${idx}.days"]`);
      const priceEl = tr.querySelector(`[data-field="items.${idx}.price"]`);
      const sumEl = tr.querySelector(`[data-field="items.${idx}.line_sum"]`);
      if (!sumEl || !qtyEl) return;
      const qty = parseMoney(qtyEl.innerText);
      const days = daysEl ? parseMoney(daysEl.innerText) || 1 : 1;
      const price = priceEl ? parseMoney(priceEl.innerText) : parseMoney(sumEl.innerText) / Math.max(1, qty * days);
      if (priceEl) {
        const line = Math.round(qty * days * price);
        sumEl.textContent = String(line).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
      }
    });
  }

  function mergeCollected(collected) {
    if (!collected) return;
    if (collected.custom_title != null) _sessionOverrides.custom_title = collected.custom_title;
    if (collected.body_notes != null) _sessionOverrides.body_notes = collected.body_notes;
    if (collected.footer_notes != null) _sessionOverrides.footer_notes = collected.footer_notes;
    if (collected.context_overrides) {
      _sessionOverrides.context_overrides = {
        ..._sessionOverrides.context_overrides,
        ...collected.context_overrides,
      };
    }
    if (collected.items) _sessionOverrides.items = collected.items;

    const titleEl = $('pv-custom-title');
    const bodyEl = $('pv-body-notes');
    const footEl = $('pv-footer-notes');
    if (titleEl && collected.custom_title != null) titleEl.value = collected.custom_title;
    if (bodyEl && collected.body_notes != null) bodyEl.value = collected.body_notes;
    if (footEl && collected.footer_notes != null) footEl.value = collected.footer_notes;
  }

  function previewOverridesPayload() {
    const titleEl = $('pv-custom-title');
    const bodyEl = $('pv-body-notes');
    const footEl = $('pv-footer-notes');
    if (titleEl) _sessionOverrides.custom_title = titleEl.value;
    if (bodyEl) _sessionOverrides.body_notes = bodyEl.value;
    if (footEl) _sessionOverrides.footer_notes = footEl.value;

    const payload = {
      custom_title: _sessionOverrides.custom_title,
      body_notes: _sessionOverrides.body_notes,
      footer_notes: _sessionOverrides.footer_notes,
    };
    if (_previewDealId) payload.deal_id = _previewDealId;
    if (_sessionOverrides.context_overrides && Object.keys(_sessionOverrides.context_overrides).length) {
      payload.context_overrides = _sessionOverrides.context_overrides;
    }
    if (_sessionOverrides.items) payload.items = _sessionOverrides.items;
    return payload;
  }

  function wireFrameEditors() {
    const frame = $('pv-frame');
    const doc = frame && (frame.contentDocument || frame.contentWindow?.document);
    if (!doc || _previewMode !== 'html') return;

    doc.querySelectorAll('[data-field][contenteditable="true"]').forEach((el) => {
      el.addEventListener('blur', () => {
        localRecalcLineSums(doc);
        clearTimeout(_blurTimer);
        _blurTimer = setTimeout(() => {
          mergeCollected(collectFromFrame());
          refreshPreviewFrame();
        }, 450);
      });
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !el.classList.contains('pv-ed-block')) {
          e.preventDefault();
          el.blur();
        }
      });
    });
  }

  async function refreshPreviewFrame() {
    if (!_previewDocType || _refreshing) return;
    _refreshing = true;
    const frame = $('pv-frame');
    const payload = previewOverridesPayload();
    revokePreviewBlob();
    try {
      if (_previewMode === 'pdf') {
        frame.removeAttribute('srcdoc');
        frame.src = 'about:blank';
        const res = await fetch(
          '/api/document-templates/' + encodeURIComponent(_previewDocType) + '/preview.pdf?inline=1',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || 'Не удалось загрузить PDF');
        }
        const blob = await res.blob();
        _previewBlobUrl = URL.createObjectURL(blob);
        frame.src = _previewBlobUrl;
      } else {
        frame.src = 'about:blank';
        const res = await fetch(
          '/api/document-templates/' + encodeURIComponent(_previewDocType) + '/preview.html',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || 'Не удалось загрузить превью');
        }
        const html = await res.text();
        frame.onload = () => {
          wireFrameEditors();
          frame.onload = null;
        };
        frame.srcdoc = html;
      }
    } catch (e) {
      frame.removeAttribute('src');
      frame.srcdoc =
        `<p style="padding:24px;font-family:sans-serif;color:#b91c1c">${escapeHtml(e.message || String(e))}</p>`;
    } finally {
      _refreshing = false;
    }
  }

  function setPreviewMode(mode, reload) {
    _previewMode = mode === 'pdf' ? 'pdf' : 'html';
    const th = $('pv-tab-html');
    const tp = $('pv-tab-pdf');
    if (th) th.classList.toggle('active', _previewMode === 'html');
    if (tp) tp.classList.toggle('active', _previewMode === 'pdf');
    if (reload !== false) refreshPreviewFrame();
  }

  async function openDocPreview(opts) {
    const docType = opts.docType || opts.doc_type;
    const dealId = opts.dealId || opts.deal_id || null;
    const mode = opts.mode || 'html';
    if (!docType) return;

    _previewDocType = docType;
    _previewDealId = dealId ? parseInt(dealId, 10) : null;
    _previewMode = mode === 'pdf' ? 'pdf' : 'html';
    _sessionOverrides = {
      custom_title: '',
      body_notes: '',
      footer_notes: '',
      context_overrides: {},
      items: null,
    };

    const titleEl = $('pv-title');
    if (titleEl) titleEl.textContent = 'Загрузка…';
    const backdrop = $('pv-drawer-backdrop');
    if (backdrop) backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';

    const regBtn = $('pv-registry-btn');
    if (regBtn) regBtn.hidden = !_previewDealId;

    setPreviewMode(_previewMode, false);
    try {
      let url = '/api/document-templates/' + encodeURIComponent(docType) + '/preview-meta';
      if (_previewDealId) url += '?deal_id=' + encodeURIComponent(_previewDealId);
      const res = await fetch(url);
      const meta = await res.json();
      if (!res.ok) throw new Error(meta.error || 'Ошибка');
      if (titleEl) titleEl.textContent = meta.name || docType;
      if ($('pv-custom-title')) $('pv-custom-title').value = meta.custom_title || '';
      if ($('pv-body-notes')) $('pv-body-notes').value = meta.body_notes || '';
      if ($('pv-footer-notes')) $('pv-footer-notes').value = meta.footer_notes || '';
      _sessionOverrides.custom_title = meta.custom_title || '';
      _sessionOverrides.body_notes = meta.body_notes || '';
      _sessionOverrides.footer_notes = meta.footer_notes || '';
      await refreshPreviewFrame();
    } catch (e) {
      alert(e.message || String(e));
      closeDocPreview();
    }
  }

  async function applyPreviewEdits() {
    const btn = $('pv-apply-btn');
    if (btn) btn.disabled = true;
    try {
      if (_previewMode === 'html') mergeCollected(collectFromFrame());
      await refreshPreviewFrame();
      showToast('Превью обновлено');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function downloadPreview(fmt) {
    if (!_previewDocType) return;
    if (_previewMode === 'html') mergeCollected(collectFromFrame());
    const payload = previewOverridesPayload();
    const url =
      fmt === 'pdf'
        ? '/api/document-templates/' + encodeURIComponent(_previewDocType) + '/preview.pdf?inline=0'
        : '/api/document-templates/' + encodeURIComponent(_previewDocType) + '/preview.docx';
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || 'Ошибка скачивания');
      }
      const blob = await res.blob();
      const a = document.createElement('a');
      const obj = URL.createObjectURL(blob);
      a.href = obj;
      const prefix = _previewDealId ? `Deal_${_previewDealId}` : 'Sample';
      a.download = `${prefix}_${_previewDocType}.${fmt === 'pdf' ? 'pdf' : 'docx'}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(obj), 2000);
      showToast('Файл скачан');
    } catch (e) {
      alert(e.message || String(e));
    }
  }

  function prepareForClient() {
    if (!_previewDocType) return;
    const base = location.origin;
    const q = _previewDealId ? ` (сделка CRM-${_previewDealId})` : '';
    const text = [
      'Документ Intro Show' + q + ':',
      '',
      'Откройте превью в CRM, примените правки и скачайте Word/PDF из панели превью.',
      `Тип: ${_previewDocType}`,
    ].join('\n');
    const subject = encodeURIComponent(
      'Документ: ' + (($('pv-title') && $('pv-title').textContent) || _previewDocType)
    );
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => showToast('Текст скопирован')).catch(() => {});
    }
    window.open('mailto:?subject=' + subject + '&body=' + encodeURIComponent(text), '_blank');
  }

  async function savePreviewToRegistry() {
    if (!_previewDealId || !_previewDocType) {
      showToast('Нет сделки для реестра');
      return;
    }
    const map = {
      estimate_internal: `/api/deals/${_previewDealId}/estimate.pdf?mode=internal`,
      estimate_client: `/api/deals/${_previewDealId}/estimate.pdf?mode=client`,
      estimate_client_priced: `/api/deals/${_previewDealId}/estimate.pdf?mode=client_priced`,
      contract: `/api/deals/${_previewDealId}/contract.pdf`,
      technichka: `/api/deals/${_previewDealId}/technichka`,
    };
    const url = map[_previewDocType];
    if (!url) {
      showToast('Сохранение — из карточки сделки');
      return;
    }
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error('Не удалось сгенерировать документ по сделке');
      const blob = await res.blob();
      const a = document.createElement('a');
      const obj = URL.createObjectURL(blob);
      a.href = obj;
      a.download = `Deal_${_previewDealId}_${_previewDocType}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(obj), 2000);
      showToast('Документ сгенерирован по сделке #' + _previewDealId);
    } catch (e) {
      alert(e.message || String(e));
    }
  }

  function closeDocPreview() {
    const backdrop = $('pv-drawer-backdrop');
    if (backdrop) backdrop.classList.remove('open');
    revokePreviewBlob();
    const frame = $('pv-frame');
    if (frame) {
      frame.src = 'about:blank';
      frame.removeAttribute('srcdoc');
    }
    _previewDocType = null;
    _previewDealId = null;
    const otherOpen = document.getElementById('tpl-editor-backdrop')?.classList.contains('open');
    if (!otherOpen) document.body.style.overflow = '';
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('pv-drawer-backdrop')?.classList.contains('open')) {
      closeDocPreview();
    }
  });

  // Public API + aliases used by existing onclick handlers
  global.DocPreview = {
    open: openDocPreview,
    close: closeDocPreview,
    apply: applyPreviewEdits,
    download: downloadPreview,
    setMode: setPreviewMode,
    refresh: refreshPreviewFrame,
    prepareForClient,
    saveToRegistry: savePreviewToRegistry,
  };
  global.openPreview = function (docType, mode) {
    let dealId = null;
    const dealSel = $('f-deal');
    if (dealSel && dealSel.value) dealId = dealSel.value;
    return openDocPreview({ docType, mode, dealId });
  };
  global.closePreview = closeDocPreview;
  global.applyPreviewEdits = applyPreviewEdits;
  global.downloadPreview = downloadPreview;
  global.setPreviewMode = setPreviewMode;
  global.prepareForClient = prepareForClient;
  global.savePreviewToRegistry = savePreviewToRegistry;
  global.refreshPreviewFrame = refreshPreviewFrame;
})(window);
