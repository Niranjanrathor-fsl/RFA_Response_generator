/*
 * Firstsource RFP Response Generator - frontend.
 *
 * This file replaces the in-browser AI bridge used by the original Cowork skill.
 * That version called Claude directly from the page; this version calls our own
 * backend, which holds the API key and the knowledge base:
 *
 *   POST /api/generate       multipart upload -> validated response document
 *   POST /api/render/{fmt}   response document -> downloadable file
 *
 * The browser never sees the Anthropic API key, the prompt or the knowledge base.
 */
(function () {
  'use strict';

  var $ = function (selector) { return document.querySelector(selector); };

  var state = {
    files: [],          // File objects queued for upload
    formats: ['dashboard'],
    config: null,
    result: null,       // { document, sources, mode, question_count, ... }
    objectUrls: []      // revoked on teardown
  };

  // ------------------------------------------------------------------ helpers
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function formatSize(bytes) {
    if (bytes < 1024) { return bytes + ' B'; }
    if (bytes < 1024 * 1024) { return (bytes / 1024).toFixed(0) + ' KB'; }
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function setStatus(message, kind) {
    var box = $('#status');
    box.className = 'status show' + (kind === 'error' ? ' error' : '');
    box.innerHTML = kind === 'busy'
      ? '<span class="spinner"></span>' + escapeHtml(message)
      : escapeHtml(message);
  }

  function clearStatus() {
    var box = $('#status');
    box.className = 'status';
    box.innerHTML = '';
  }

  function extensionOf(name) {
    var index = name.lastIndexOf('.');
    return index < 0 ? '' : name.slice(index).toLowerCase();
  }

  // ------------------------------------------------------------- file queueing
  function renderChips() {
    var container = $('#chips');
    if (!state.files.length) { container.innerHTML = ''; return; }

    var html = state.files.map(function (file, index) {
      return '<span class="filechip">'
        + '<span class="name">' + escapeHtml(file.name) + '</span>'
        + '<span class="meta">' + formatSize(file.size) + '</span>'
        + '<button type="button" data-remove="' + index + '" title="Remove">&#10005;</button>'
        + '</span>';
    }).join('');

    if (state.files.length > 1) {
      var total = state.files.reduce(function (sum, file) { return sum + file.size; }, 0);
      html += '<div class="chips-summary"><b>' + state.files.length + ' documents queued</b> &mdash; '
        + formatSize(total) + ' total. They will be merged into one response, with answers '
        + 'attributed to their source document. '
        + '<a href="#" data-clear="1">Clear all</a></div>';
    }
    container.innerHTML = html;
  }

  function addFiles(fileList) {
    var incoming = Array.prototype.slice.call(fileList || []);
    if (!incoming.length) { return; }

    var maxFiles = (state.config && state.config.max_files) || 20;
    var maxBytes = ((state.config && state.config.max_upload_mb) || 25) * 1024 * 1024;
    var accepted = state.config && state.config.accepted_extensions;
    var rejected = [];

    incoming.forEach(function (file) {
      var duplicate = state.files.some(function (existing) {
        return existing.name === file.name && existing.size === file.size;
      });
      if (duplicate) { return; }
      if (accepted && accepted.length && accepted.indexOf(extensionOf(file.name)) === -1) {
        rejected.push(file.name + ' (unsupported type)');
        return;
      }
      if (file.size > maxBytes) {
        rejected.push(file.name + ' (over ' + ((state.config && state.config.max_upload_mb) || 25) + ' MB)');
        return;
      }
      if (state.files.length >= maxFiles) {
        rejected.push(file.name + ' (limit of ' + maxFiles + ' files reached)');
        return;
      }
      state.files.push(file);
    });

    renderChips();
    if (rejected.length) {
      setStatus('Skipped: ' + rejected.join(', ') + '.', 'error');
    } else {
      clearStatus();
    }
  }

  function wireDropzone() {
    var dropzone = $('#dropzone');
    var input = $('#fileInput');

    dropzone.addEventListener('click', function () { input.click(); });
    dropzone.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () {
      addFiles(input.files);
      input.value = '';   // allow re-adding the same file after removing it
    });

    ['dragover', 'dragenter'].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add('hover');
      });
    });
    ['dragleave', 'dragend', 'drop'].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.remove('hover');
      });
    });
    dropzone.addEventListener('drop', function (event) {
      addFiles(event.dataTransfer && event.dataTransfer.files);
    });

    $('#chips').addEventListener('click', function (event) {
      var target = event.target;
      if (target.dataset && target.dataset.remove !== undefined) {
        state.files.splice(parseInt(target.dataset.remove, 10), 1);
        renderChips();
      } else if (target.dataset && target.dataset.clear) {
        event.preventDefault();
        state.files = [];
        renderChips();
      }
    });
  }

  function wireFormatPicker() {
    Array.prototype.forEach.call(document.querySelectorAll('.fmt'), function (button) {
      button.addEventListener('click', function () {
        var fmt = button.dataset.fmt;
        var index = state.formats.indexOf(fmt);
        if (index >= 0) {
          if (state.formats.length === 1) { return; }   // always keep one selected
          state.formats.splice(index, 1);
          button.classList.remove('active');
        } else {
          state.formats.push(fmt);
          button.classList.add('active');
        }
      });
    });
  }

  // ----------------------------------------------------------------- requests
  function describeError(response, payload) {
    if (payload && payload.detail) {
      return typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail);
    }
    if (response.status === 401) { return 'Your session has expired. Please sign in again.'; }
    if (response.status === 413) { return 'The upload was too large for the server.'; }
    if (response.status === 502) { return 'The model service could not be reached. Try again shortly.'; }
    return 'Request failed with status ' + response.status + '.';
  }

  async function parseJsonSafely(response) {
    try { return await response.json(); } catch (error) { return null; }
  }

  async function generate() {
    var pasted = $('#pasteBox').value.trim();
    if (!state.files.length && !pasted) {
      setStatus('Add at least one document or paste some content first.', 'error');
      return;
    }

    var form = new FormData();
    state.files.forEach(function (file) { form.append('files', file, file.name); });
    form.append('pasted', pasted);
    form.append('title', $('#titleInput').value.trim());
    form.append('audience', $('#audience').value);

    var sourceCount = state.files.length + (pasted ? 1 : 0);
    $('#generateBtn').disabled = true;
    setStatus(
      sourceCount > 1
        ? 'Reading ' + sourceCount + ' documents and generating a merged response…'
        : 'Reading your content and generating your response…',
      'busy'
    );

    try {
      var response = await fetch('/api/generate', {
        method: 'POST',
        body: form,
        credentials: 'same-origin'
      });
      var payload = await parseJsonSafely(response);

      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) {
        setStatus(describeError(response, payload), 'error');
        return;
      }

      state.result = payload;
      clearStatus();
      await showResults();
    } catch (error) {
      setStatus('Could not reach the server: ' + (error && error.message ? error.message : error), 'error');
    } finally {
      $('#generateBtn').disabled = false;
    }
  }

  async function renderFormat(fmt) {
    var response = await fetch('/api/render/' + encodeURIComponent(fmt), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        document: state.result.document,
        sources: state.result.sources || []
      })
    });
    if (!response.ok) {
      var payload = await parseJsonSafely(response);
      throw new Error(describeError(response, payload));
    }
    var disposition = response.headers.get('X-Output-Filename') || (fmt + '-response');
    return { blob: await response.blob(), filename: disposition };
  }

  function downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    state.objectUrls.push(url);
    var link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  // ------------------------------------------------------------------- views
  function showLogin() {
    $('#intake').hidden = true;
    $('#results').classList.remove('show');
    $('#loginCard').hidden = false;
  }

  function showIntake() {
    $('#loginCard').hidden = true;
    $('#results').classList.remove('show');
    $('#intake').hidden = false;
  }

  async function showResults() {
    var result = state.result;
    var doc = result.document;

    $('#resultTitle').textContent = doc.title || 'Firstsource response';
    $('#resultSubtitle').textContent = doc.subtitle || '';

    var badges = [];
    badges.push({
      text: result.mode === 'rfi'
        ? result.question_count + ' question' + (result.question_count === 1 ? '' : 's') + ' answered'
        : (doc.tabs || []).length + ' section' + ((doc.tabs || []).length === 1 ? '' : 's'),
      accent: true
    });
    if ((result.sources || []).length) {
      badges.push({ text: result.sources.length + ' source document' + (result.sources.length === 1 ? '' : 's') });
    }
    if (result.model) { badges.push({ text: result.model }); }
    $('#resultBadges').innerHTML = badges.map(function (badge) {
      return '<span class="badge' + (badge.accent ? ' accent' : '') + '">' + escapeHtml(badge.text) + '</span>';
    }).join('');

    var notices = [];
    if (result.truncated) {
      notices.push({
        kind: 'warn',
        text: 'Your source content was long and was truncated before being sent to the model. '
            + 'For very large documents, split them or generate section by section.'
      });
    }
    (result.sources || []).forEach(function (source) {
      if (source.note) {
        notices.push({ kind: 'warn', text: source.name + ': ' + source.note });
      }
    });
    $('#resultNotices').innerHTML = notices.map(function (notice) {
      return '<div class="notice ' + notice.kind + '">' + escapeHtml(notice.text) + '</div>';
    }).join('');

    // One download button per requested format, plus previewing the dashboard inline.
    var container = $('#downloads');
    container.innerHTML = '<span class="label">Download:</span>';
    var labels = (state.config && state.config.formats) || {};
    state.formats.forEach(function (fmt) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn secondary';
      button.textContent = labels[fmt] || fmt;
      button.addEventListener('click', async function () {
        var original = button.textContent;
        button.disabled = true;
        button.textContent = 'Building…';
        try {
          var file = await renderFormat(fmt);
          downloadBlob(file.blob, file.filename);
          button.textContent = original;
        } catch (error) {
          button.textContent = original;
          setStatus(error.message || String(error), 'error');
        } finally {
          button.disabled = false;
        }
      });
      container.appendChild(button);
    });

    $('#intake').hidden = true;
    $('#results').classList.add('show');
    window.scrollTo(0, 0);

    if (state.formats.indexOf('dashboard') >= 0 || state.formats.indexOf('qa') >= 0) {
      var previewFormat = state.formats.indexOf('dashboard') >= 0 ? 'dashboard' : 'qa';
      try {
        var preview = await renderFormat(previewFormat);
        var html = await preview.blob.text();
        $('#previewWrap').hidden = false;
        $('#preview').srcdoc = html;
      } catch (error) {
        $('#previewWrap').hidden = true;
      }
    } else {
      $('#previewWrap').hidden = true;
    }
  }

  function restart() {
    state.files = [];
    state.result = null;
    state.objectUrls.forEach(URL.revokeObjectURL);
    state.objectUrls = [];
    $('#pasteBox').value = '';
    $('#titleInput').value = '';
    $('#preview').srcdoc = '';
    $('#previewWrap').hidden = true;
    renderChips();
    clearStatus();
    showIntake();
  }

  // -------------------------------------------------------------------- boot
  async function boot() {
    $('#footerCopyright').textContent =
      'Copyright © ' + new Date().getFullYear() + ' Firstsource. All rights reserved.';

    wireDropzone();
    wireFormatPicker();
    $('#generateBtn').addEventListener('click', generate);
    $('#restartBtn').addEventListener('click', restart);

    try {
      var configResponse = await fetch('/api/config', { credentials: 'same-origin' });
      state.config = await configResponse.json();
      if (state.config.accepted_extensions) {
        $('#dropzoneSub').textContent =
          state.config.accepted_extensions.slice(0, 8).join(' ')
          + ' — or click to browse (up to ' + state.config.max_files + ' files, '
          + state.config.max_upload_mb + ' MB each)';
      }
    } catch (error) {
      state.config = null;
    }

    var authMode = state.config ? state.config.auth_mode : 'disabled';
    if (authMode === 'disabled') {
      showIntake();
      return;
    }

    try {
      var meResponse = await fetch('/auth/me', { credentials: 'same-origin' });
      var me = await meResponse.json();
      if (me.authenticated) {
        $('#who').innerHTML = escapeHtml((me.user && me.user.name) || 'Signed in')
          + '<br><a href="/auth/logout">Sign out</a>';
        showIntake();
      } else {
        showLogin();
      }
    } catch (error) {
      showLogin();
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
