/*
 * Firstsource RFP Response Generator - frontend (2026 wizard redesign).
 *
 * This calls our own backend, which holds the API key and the knowledge base:
 *
 *   POST /api/generate       multipart upload -> validated response document
 *   POST /api/render/{fmt}   response document -> downloadable file
 *
 * The browser never sees the Azure OpenAI key, the prompt or the knowledge base.
 * The visual design (sidebar + 4-step wizard) came from a separate static-HTML
 * export; this file keeps that markup/CSS but replaces its logic with real
 * calls to the endpoints above (the export had no backend wiring at all - it
 * called Claude directly from the browser and built Office files client-side).
 */
(function () {
  'use strict';

  var $ = function (selector) { return document.querySelector(selector); };
  var LOGO_WHITE = '/static/logos/Firstsource-logo-white.png';

  var state = {
    files: [],          // File objects queued for upload
    formats: ['dashboard'],
    config: null,
    result: null,       // { document, sources, mode, question_count, ... }
    objectUrls: [],     // revoked on teardown
    step: 0
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
    box.className = kind === 'error' ? 'show err' : 'show';
    box.innerHTML = kind === 'busy'
      ? '<span class="spinner"></span>' + escapeHtml(message)
      : escapeHtml(message);
  }

  function clearStatus() {
    var box = $('#status');
    box.className = '';
    box.innerHTML = '';
  }

  function extensionOf(name) {
    var index = name.lastIndexOf('.');
    return index < 0 ? '' : name.slice(index).toLowerCase();
  }

  // ------------------------------------------------------------- file queueing
  function renderChips() {
    var container = $('#chips');
    if (!state.files.length) { container.innerHTML = ''; updateReadyState(); return; }

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
    updateReadyState();
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
    var dropzone = $('#dz');
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

    $('#pasteBox').addEventListener('input', function () {
      $('#charCount').textContent = $('#pasteBox').value.length.toLocaleString() + ' characters';
      updateReadyState();
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
        updateReadyState();
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
      showPane(0);
      return;
    }

    var form = new FormData();
    state.files.forEach(function (file) { form.append('files', file, file.name); });
    form.append('pasted', pasted);
    form.append('title', $('#titleIn').value.trim());
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

  // --------------------------------------------------------- result preview
  // Pure display helpers: render the SAME JSON shape /api/generate returns
  // (title/subtitle/metrics/tabs with intro/bullets/table/qa/callout) as a
  // book-style preview. No network calls of their own - actual downloads for
  // every format (including dashboard/qa) always go through renderFormat().
  var ICONS = {
    doc: '<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
    bulb: '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12c1 1 1 2 1 3h6c0-1 0-2 1-3a7 7 0 0 0-4-12z"/>',
    layers: '<path d="M12 2l9 5-9 5-9-5z"/><path d="M3 12l9 5 9-5M3 17l9 5 9-5"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3 3-5 6-5s6 2 6 5"/><path d="M16 5a3 3 0 0 1 0 6M18 20c0-2-1-3.5-2.5-4.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    growth: '<path d="M3 17l6-6 4 4 8-8"/><path d="M21 7h-5M21 7v5"/>',
    lock: '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>'
  };
  var ICON_ORDER = ['doc', 'chart', 'target', 'layers', 'gear', 'bulb', 'shield', 'users', 'clock', 'growth', 'flag', 'check'];
  function svg(name) {
    var d = ICONS[name] || ICONS.doc;
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>';
  }
  function pickIcon(text, i) {
    var t = (text || '').toLowerCase();
    if (/overview|summary/.test(t)) return 'doc';
    if (/response|answer|question|q&a|rfp|rfi/.test(t)) return 'check';
    if (/proof|outcome|result|impact|roi|metric/.test(t)) return 'target';
    if (/approach|method|process|engage|deliver/.test(t)) return 'gear';
    if (/capab|platform|architecture|tech|solution|kairos/.test(t)) return 'layers';
    if (/innovat|lab|emerging|future|research/.test(t)) return 'bulb';
    if (/security|risk|complian|govern|liab/.test(t)) return 'shield';
    if (/team|people|talent|resource|staff/.test(t)) return 'users';
    if (/roadmap|timeline|phase|plan|schedule/.test(t)) return 'clock';
    if (/growth|portfolio|scale|revenue|adoption/.test(t)) return 'chart';
    if (/differenti|why|advantage|win/.test(t)) return 'flag';
    return ICON_ORDER[i % ICON_ORDER.length];
  }

  function renderDashboardPreview(doc) {
    var metrics = (doc.metrics || []).slice(0, 8).map(function (m, i) {
      return '<div class="metric"><div class="micon">' + svg(m.icon || pickIcon(m.label, i)) + '</div>'
        + '<div class="val' + (m.accent ? ' accent' : '') + '">' + escapeHtml(m.value) + '</div>'
        + '<div class="lbl">' + escapeHtml(m.label) + '</div></div>';
    }).join('');
    var tabs = doc.tabs || [];
    var btns = tabs.map(function (t, i) {
      return '<button class="' + (i === 0 ? 'active' : '') + '" data-i="' + i + '"><span class="nicon">'
        + svg(t.icon || pickIcon(t.name, i)) + '</span>' + escapeHtml(t.name || ('Tab ' + (i + 1))) + '</button>';
    }).join('');
    var panels = tabs.map(function (t, i) {
      var body = '';
      if (t.intro) { body += '<p class="intro">' + escapeHtml(t.intro) + '</p>'; }
      if (t.qa && t.qa.length) {
        body += t.qa.map(function (x) {
          return '<div class="qa"><div class="qq">' + escapeHtml(x.n ? x.n + '. ' : '') + escapeHtml(x.q || '') + '</div>'
            + '<div class="qans">' + escapeHtml(x.a || '') + '</div></div>';
        }).join('');
      }
      if (t.bullets && t.bullets.length) {
        body += '<ul class="clean">' + t.bullets.map(function (b) { return '<li>' + escapeHtml(b) + '</li>'; }).join('') + '</ul>';
      }
      if (t.table && t.table.headers) {
        body += '<table><thead><tr>' + t.table.headers.map(function (c) { return '<th>' + escapeHtml(c) + '</th>'; }).join('') + '</tr></thead>'
          + '<tbody>' + (t.table.rows || []).map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + escapeHtml(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table>';
      }
      if (t.callout && t.callout.body) {
        body += '<div class="callout"><b>' + escapeHtml(t.callout.title || 'Key takeaway') + ':</b> ' + escapeHtml(t.callout.body) + '</div>';
      }
      return '<section class="panel' + (i === 0 ? ' active' : '') + '" data-i="' + i + '"><h3>' + escapeHtml(t.name) + '</h3><div class="rule"></div>' + body + '</section>';
    }).join('');
    var year = new Date().getFullYear();
    var html = '<div class="d-head"><div><h2>' + escapeHtml(doc.title || 'Executive Dashboard') + '</h2><p>' + escapeHtml(doc.subtitle || '') + '</p></div>'
      + '<img src="' + LOGO_WHITE + '" alt="Firstsource" style="height:26px;width:auto"></div>'
      + '<div class="metrics">' + metrics + '</div>'
      + '<div class="book"><nav class="sidenav">' + btns + '</nav><div class="content"><div class="panels">' + panels + '</div></div></div>'
      + '<div class="d-foot">Copyright &copy; ' + year + ' Firstsource. All rights reserved.<span class="chip">Intelligence that Operates</span></div>';
    $('#dashboard').innerHTML = html;
    var buttons = $('#dashboard').querySelectorAll('.sidenav button');
    var sections = $('#dashboard').querySelectorAll('.panel');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        buttons.forEach(function (b) { b.classList.remove('active'); });
        sections.forEach(function (s) { s.classList.remove('active'); });
        btn.classList.add('active');
        $('#dashboard').querySelector('.panel[data-i="' + btn.dataset.i + '"]').classList.add('active');
      });
    });
  }

  function renderQaPreview(doc) {
    var qaList = null, tbl = null;
    (doc.tabs || []).forEach(function (t) {
      if (!qaList && t.qa && t.qa.length) { qaList = t.qa; }
      if (!tbl && t.table && t.table.headers) { tbl = t.table; }
    });
    var body = '';
    if (qaList) {
      body = qaList.map(function (x) {
        return '<div class="qa"><div class="qq">' + escapeHtml(x.n ? x.n + '. ' : '') + escapeHtml(x.q || '') + '</div>'
          + '<div class="qans">' + escapeHtml(x.a || '') + '</div></div>';
      }).join('');
    } else if (tbl) {
      body = '<table><thead><tr>' + tbl.headers.map(function (c) { return '<th>' + escapeHtml(c) + '</th>'; }).join('') + '</tr></thead>'
        + '<tbody>' + (tbl.rows || []).map(function (r) {
          return '<tr>' + r.map(function (c) { return '<td>' + escapeHtml(c) + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table>';
    } else {
      (doc.tabs || []).forEach(function (t) {
        body += '<h3>' + escapeHtml(t.name) + '</h3>';
        if (t.intro) { body += '<p class="intro">' + escapeHtml(t.intro) + '</p>'; }
        if (t.bullets && t.bullets.length) {
          body += '<ul class="clean">' + t.bullets.map(function (b) { return '<li>' + escapeHtml(b) + '</li>'; }).join('') + '</ul>';
        }
      });
    }
    var year = new Date().getFullYear();
    $('#dashboard').innerHTML = '<div class="d-head"><div><h2>' + escapeHtml(doc.title || 'Response to Questions') + '</h2>'
      + '<p>' + escapeHtml(doc.subtitle || '') + '</p></div><img src="' + LOGO_WHITE + '" alt="Firstsource" style="height:26px;width:auto"></div>'
      + '<div class="panels">' + body + '</div>'
      + '<div class="d-foot">Copyright &copy; ' + year + ' Firstsource. All rights reserved.<span class="chip">Intelligence that Operates</span></div>';
  }

  function successCard(doc) {
    var year = new Date().getFullYear();
    $('#dashboard').innerHTML = '<div class="d-head"><div><h2>Files ready</h2><p>' + escapeHtml(doc.title || '') + '</p></div>'
      + '<img src="' + LOGO_WHITE + '" alt="Firstsource" style="height:26px;width:auto"></div>'
      + '<div class="panels"><p class="intro">Use the download button(s) above to save your requested format(s).</p></div>'
      + '<div class="d-foot">Copyright &copy; ' + year + ' Firstsource. All rights reserved.<span class="chip">Intelligence that Operates</span></div>';
  }

  // ------------------------------------------------------------------- views
  function showLogin() {
    $('#app').hidden = true;
    $('#loginCard').hidden = false;
    // "Sign in with SSO" is only true in OIDC mode; password mode shows a form.
    var mode = state.config && state.config.auth_mode;
    if (mode === 'password') {
      var button = $('#loginBtn');
      if (button) { button.textContent = 'Sign in'; }
      var blurb = $('#loginCard') && $('#loginCard').querySelector('p');
      if (blurb) {
        blurb.textContent = 'This application is restricted. Sign in with the username and password your administrator gave you.';
      }
    }
  }

  function showApp() {
    $('#loginCard').hidden = true;
    $('#app').hidden = false;
  }

  async function showResults() {
    var result = state.result;
    var doc = result.document;

    var quality = result.quality;
    if (quality && quality.flag) {
      var qLabel = quality.flag === 'green' ? 'Well-grounded' : quality.flag === 'amber' ? 'Review recommended' : 'Verify carefully';
      $('#qualityBadge').innerHTML = '<span class="quality-badge ' + quality.flag + '">'
        + '<span class="qb-dot"></span>Quality score: ' + quality.score + '/100 &middot; ' + escapeHtml(qLabel)
        + (quality.reason ? '<span class="qb-reason"> &mdash; ' + escapeHtml(quality.reason) + '</span>' : '')
        + '</span>';
    } else {
      $('#qualityBadge').innerHTML = '';
    }

    var notices = [];
    if (result.truncated) {
      notices.push('Your source content was long and was truncated before being sent to the model. '
        + 'For very large documents, split them or generate section by section.');
    }
    (result.sources || []).forEach(function (source) {
      if (source.note) { notices.push(source.name + ': ' + source.note); }
    });
    $('#resultNotices').innerHTML = notices.map(function (text) {
      return '<div class="notice warn">' + escapeHtml(text) + '</div>';
    }).join('');

    var labels = (state.config && state.config.formats) || {};
    var actionsHtml = '<button class="btn secondary" id="againBtn">&larr; Build another</button>';
    state.formats.forEach(function (fmt, i) {
      actionsHtml += '<button type="button" class="btn" id="dlF' + i + '">' + escapeHtml(labels[fmt] || fmt) + '</button>';
    });
    $('#dashActions').innerHTML = actionsHtml;
    $('#againBtn').addEventListener('click', restart);
    state.formats.forEach(function (fmt, i) {
      var button = document.getElementById('dlF' + i);
      button.addEventListener('click', async function () {
        var original = button.textContent;
        button.disabled = true;
        button.textContent = 'Building…';
        try {
          var file = await renderFormat(fmt);
          downloadBlob(file.blob, file.filename);
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        } finally {
          button.textContent = original;
          button.disabled = false;
        }
      });
    });

    if (state.formats.indexOf('dashboard') >= 0) {
      renderDashboardPreview(doc);
    } else if (state.formats.indexOf('qa') >= 0) {
      renderQaPreview(doc);
    } else {
      successCard(doc);
    }

    $('#intake').style.display = 'none';
    $('#dash').classList.add('show');
    window.scrollTo(0, 0);
  }

  function restart() {
    state.files = [];
    state.result = null;
    state.objectUrls.forEach(URL.revokeObjectURL);
    state.objectUrls = [];
    $('#pasteBox').value = '';
    $('#titleIn').value = '';
    $('#charCount').textContent = '0 characters';
    $('#qualityBadge').innerHTML = '';
    renderChips();
    clearStatus();
    $('#dash').classList.remove('show');
    $('#intake').style.display = 'flex';
    showPane(0);
  }

  // ---------------------------------------------------------------- wizard
  var PANES = [
    { kicker: 'STEP 01', title: 'Add your input', next: 'Choose format' },
    { kicker: 'STEP 02', title: 'Choose output format', next: 'Title & audience' },
    { kicker: 'STEP 03', title: 'Title & audience', next: 'Review & generate' },
    { kicker: 'STEP 04', title: 'Review & generate', next: '' }
  ];

  function hasInput() {
    return state.files.length > 0 || $('#pasteBox').value.trim().length > 0;
  }
  function hasFormat() { return state.formats.length > 0; }
  function srcSummary() {
    var bits = [];
    if (state.files.length) { bits.push(state.files.length + (state.files.length === 1 ? ' document' : ' documents')); }
    var pasted = $('#pasteBox').value.trim();
    if (pasted) { bits.push(pasted.length.toLocaleString() + ' pasted characters'); }
    return bits.length ? bits.join(' + ') : 'Nothing added yet';
  }
  function fmtLabels() {
    return Array.prototype.slice.call(document.querySelectorAll('.fmt.active .fmt-label')).map(function (e) { return e.textContent; });
  }
  function updateReadyState() {
    var hi = hasInput(), hf = hasFormat(), ready = hi && hf;
    var doneCount = [hi, hf, ready].filter(Boolean).length;
    $('#readyPill').textContent = ready ? 'Ready to generate' : hi ? 'Pick an output format' : 'Waiting for input';
    $('#progressFill').style.width = Math.round((doneCount / 3) * 100) + '%';
    Array.prototype.forEach.call(document.querySelectorAll('.sb-item'), function (el) {
      var step = +el.dataset.step;
      var done = (step === 0 && hi) || (step === 1 && hf) || (step === 2 && hi && hf);
      el.classList.toggle('done', done && step !== state.step);
    });
  }
  function updateReview() {
    $('#revSources').textContent = srcSummary();
    var labels = fmtLabels();
    $('#revFormats').textContent = labels.length ? labels.join(' \u00b7 ') : 'No format selected';
    $('#revTitle').textContent = $('#titleIn').value.trim() || 'Untitled response';
    var audienceEl = $('#audience');
    $('#revAudience').textContent = audienceEl.options[audienceEl.selectedIndex].text;
  }
  function showPane(i) {
    state.step = Math.max(0, Math.min(PANES.length - 1, i));
    Array.prototype.forEach.call(document.querySelectorAll('.mpane'), function (p) {
      p.classList.toggle('active', +p.dataset.pane === state.step);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.sb-item'), function (el) {
      el.classList.toggle('active', +el.dataset.step === state.step);
    });
    $('#paneKicker').textContent = PANES[state.step].kicker;
    $('#paneTitle').textContent = PANES[state.step].title;
    $('#prevBtn').hidden = state.step === 0;
    $('#nextBtn').hidden = state.step === PANES.length - 1;
    if (PANES[state.step].next) { $('#nextBtn').innerHTML = escapeHtml(PANES[state.step].next) + ' &rsaquo;'; }
    if (state.step === 3) { updateReview(); }
    updateReadyState();
    window.scrollTo(0, 0);
  }
  function wireWizard() {
    Array.prototype.forEach.call(document.querySelectorAll('.sb-item'), function (el) {
      el.addEventListener('click', function () { showPane(+el.dataset.step); });
    });
    $('#prevBtn').addEventListener('click', function () { showPane(state.step - 1); });
    $('#nextBtn').addEventListener('click', function () { showPane(state.step + 1); });
    $('#homeBtn').addEventListener('click', function () { showPane(0); });
  }

  // -------------------------------------------------------------------- boot
  async function boot() {
    $('#footerCopyright').textContent =
      'Copyright © ' + new Date().getFullYear() + ' Firstsource. All rights reserved.';

    wireDropzone();
    wireFormatPicker();
    wireWizard();
    $('#generateBtn').addEventListener('click', generate);
    showPane(0);

    try {
      var configResponse = await fetch('/api/config', { credentials: 'same-origin' });
      state.config = await configResponse.json();
      // Show the server's curated groups, not the alphabetically-first eight raw
      // extensions - that listed things like .dotx and .log while omitting PDF
      // and PowerPoint entirely.
      if (state.config.accepted_display) {
        $('#dzSub').textContent =
          state.config.accepted_display.join('  \u00b7  ')
          + '  \u2014 up to ' + state.config.max_files + ' files, '
          + state.config.max_upload_mb + ' MB each';
      }
      // Keep the picker's filter in step with what the server actually accepts,
      // so the hardcoded accept="" list cannot drift out of date again.
      if (state.config.accepted_extensions) {
        var fileInput = $('#fileInput');
        if (fileInput) {
          fileInput.setAttribute('accept', state.config.accepted_extensions.join(','));
        }
      }
    } catch (error) {
      state.config = null;
    }

    var authMode = state.config ? state.config.auth_mode : 'disabled';
    if (authMode === 'disabled') {
      showApp();
      return;
    }

    try {
      var meResponse = await fetch('/auth/me', { credentials: 'same-origin' });
      var me = await meResponse.json();
      if (me.authenticated) {
        $('#who').innerHTML = escapeHtml((me.user && me.user.name) || 'Signed in')
          + '<br><a href="/auth/logout">Sign out</a>';
        showApp();
      } else {
        showLogin();
      }
    } catch (error) {
      showLogin();
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();

