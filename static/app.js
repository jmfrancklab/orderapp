/* Order sheet behaviour. One delegated listener per event type — no per-row
   handlers, so the page stays snappy no matter how many rows exist. */
(function () {
  "use strict";

  var saveState = document.getElementById("save-state");
  var timers = {};          // "rowId:field" -> debounce timer
  var pending = 0;

  function setState(txt) { if (saveState) saveState.textContent = txt; }

  function post(url, method, body, onOk) {
    pending++; setState("saving…");
    fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (data) {
      if (onOk) onOk(data);
    }).catch(function () {
      setState("save failed — retrying on next edit");
      pending = 1; // keep the warning until something succeeds
    }).finally(function () {
      pending--; if (pending <= 0) { pending = 0; setState("saved"); }
    });
  }

  function rowOf(el) { return el.closest(".order-row"); }

  function saveField(input) {
    var row = rowOf(input);
    if (!row) return;
    var field = input.dataset.field;
    var body = {};
    body[field] = input.value;
    post("/api/orders/" + row.dataset.id, "POST", body);
  }

  function debounceSave(input) {
    var row = rowOf(input);
    if (!row || !input.dataset.field) return;
    var key = row.dataset.id + ":" + input.dataset.field;
    clearTimeout(timers[key]);
    timers[key] = setTimeout(function () { saveField(input); }, 400);
  }

  /* --- vendor match popup -------------------------------------------- */

  var _vendorPopup = null;
  var _vendorOverlay = null;
  var _popupRow = null;
  var _popupExtracted = null;
  var _popupCandidates = [];
  var _popupHintDomains = [];

  function showVendorPopup(row, data) {
    closeVendorPopup();        // clear any existing popup BEFORE setting state
    _popupRow = row;           // must come AFTER closeVendorPopup (which nulls _popupRow)
    _popupExtracted = data.extracted || null;
    _popupCandidates = data.fuzzy_candidates || [];
    _popupHintDomains = data.hint_domains || [];
    var hintDomains = _popupHintDomains;

    var overlay = document.createElement('div');
    overlay.className = 'vendor-overlay';
    overlay.onclick = closeVendorPopup;
    document.body.appendChild(overlay);
    _vendorOverlay = overlay;

    var pop = document.createElement('div');
    pop.className = 'vendor-popup';
    _vendorPopup = pop;

    var head = document.createElement('div');
    head.className = 'vendor-popup-head';
    var title = document.createElement('strong');
    title.textContent = 'Vendor not found — confirm match';
    var xBtn = document.createElement('button');
    xBtn.type = 'button'; xBtn.className = 'vendor-popup-x';
    xBtn.textContent = '×'; xBtn.onclick = closeVendorPopup;
    head.appendChild(title); head.appendChild(xBtn);
    pop.appendChild(head);

    // Build the "from quote" section from extracted address + domain hints
    var ext = _popupExtracted;
    var infoLines = [];
    if (ext) {
      if (ext.name)    infoLines.push(ext.name);
      if (ext.address) infoLines.push(ext.address);
      if (ext.phone)   infoLines.push('Phone: ' + ext.phone);
      if (ext.website) infoLines.push('Web: ' + ext.website);
    }
    // Append any domain hints not already shown
    hintDomains.forEach(function (d) {
      var already = ext && ext.website === d;
      if (!already) infoLines.push('Domain seen: ' + d);
    });
    if (infoLines.length > 0) {
      var extSec = document.createElement('div');
      extSec.className = 'vendor-popup-section';
      var extLbl = document.createElement('div');
      extLbl.className = 'vendor-popup-label';
      extLbl.textContent = 'From the quote';
      extSec.appendChild(extLbl);
      infoLines.forEach(function (line) {
        var p = document.createElement('div');
        p.className = 'vendor-popup-info'; p.textContent = line;
        extSec.appendChild(p);
      });
      pop.appendChild(extSec);
    }

    if (_popupCandidates.length > 0) {
      var candSec = document.createElement('div');
      candSec.className = 'vendor-popup-section';
      var candLbl = document.createElement('div');
      candLbl.className = 'vendor-popup-label';
      candLbl.textContent = 'Close matches in database';
      candSec.appendChild(candLbl);
      _popupCandidates.forEach(function (v, i) {
        var lbl = document.createElement('label');
        lbl.className = 'vendor-popup-candidate';
        var radio = document.createElement('input');
        radio.type = 'radio'; radio.name = 'vp-cand';
        radio.value = String(v.id);
        if (i === 0) radio.checked = true;
        lbl.appendChild(radio);
        lbl.appendChild(document.createTextNode(
          ' ' + v.name + ' — ' + Math.round(v.score * 100) + '% match'));
        candSec.appendChild(lbl);
      });
      pop.appendChild(candSec);
    }

    var actions = document.createElement('div');
    actions.className = 'vendor-popup-actions';

    if (_popupCandidates.length > 0) {
      actions.appendChild(makePopupBtn('Use database info', 'submit-btn',
        function () { vendorPopupAction('use_db'); }));
      actions.appendChild(makePopupBtn('Update vendor info in database', 'mini',
        function () { vendorPopupAction('update_db'); }));
    }

    // "Create new" is available whenever we have a name or at least a domain hint
    var canCreate = (_popupExtracted && _popupExtracted.name) || _popupHintDomains.length > 0;
    if (canCreate) {
      var noMatchLabel = _popupCandidates.length > 0
        ? 'Not a match — create new' : 'Create new vendor';
      actions.appendChild(makePopupBtn(noMatchLabel, 'mini',
        function () { vendorPopupAction('create_new'); }));
    } else if (_popupCandidates.length > 0) {
      actions.appendChild(makePopupBtn('Not a match', 'mini', closeVendorPopup));
    }

    actions.appendChild(makePopupBtn('Cancel', 'mini', closeVendorPopup));
    pop.appendChild(actions);
    document.body.appendChild(pop);
  }

  function makePopupBtn(label, cls, onClick) {
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = cls;
    btn.textContent = label; btn.onclick = onClick;
    return btn;
  }

  function closeVendorPopup() {
    if (_vendorPopup) { _vendorPopup.remove(); _vendorPopup = null; }
    if (_vendorOverlay) { _vendorOverlay.remove(); _vendorOverlay = null; }
    _popupRow = null;
  }

  function selectedCandidateId() {
    if (!_vendorPopup) return null;
    var radio = _vendorPopup.querySelector('input[name="vp-cand"]:checked');
    return radio ? parseInt(radio.value, 10) : null;
  }

  function findCandidate(id) {
    for (var i = 0; i < _popupCandidates.length; i++) {
      if (_popupCandidates[i].id === id) return _popupCandidates[i];
    }
    return null;
  }

  function vendorPopupAction(action) {
    var row = _popupRow;
    var ext = _popupExtracted;
    var vid = selectedCandidateId();
    var candidate = vid ? findCandidate(vid) : null;

    if ((action === 'use_db' || action === 'update_db') && candidate) {
      if (action === 'update_db') {
        var patch = {};
        if (ext && ext.address) patch.address = ext.address;
        if (ext && ext.phone)   patch.phone   = ext.phone;
        if (ext && ext.website) patch.website = ext.website;
        if (Object.keys(patch).length > 0) {
          post('/api/vendors/' + candidate.id + '/patch', 'PATCH', patch, null);
        }
      }
      assignVendorToRow(row, candidate);
      closeVendorPopup();
    } else if (action === 'create_new') {
      // Use extracted name, or fall back to first domain hint as the vendor name
      var newName = (ext && ext.name) || (_popupHintDomains.length > 0 ? _popupHintDomains[0] : null);
      if (!newName) return;
      var body = { name: newName };
      if (ext && ext.address) body.address = ext.address;
      if (ext && ext.phone)   body.phone   = ext.phone;
      var website = (ext && ext.website) || (_popupHintDomains.length > 0 ? _popupHintDomains[0] : null);
      if (website) body.website = website;
      post('/api/vendors', 'POST', body, function (data) {
        if (data && data.id) {
          addVendorOption(data);
          assignVendorToRow(row, data);
        }
      });
      closeVendorPopup();
    }
  }

  function assignVendorToRow(row, vendor) {
    if (!row) return;
    var vid = String(vendor.id);
    var select = row.querySelector('.vendor-select');
    if (select) {
      if (!select.querySelector('option[value="' + vid + '"]')) {
        var opt = document.createElement('option');
        opt.value = vid; opt.textContent = vendor.name;
        if (vendor.incomplete) opt.dataset.incomplete = '1';
        select.appendChild(opt);
      }
      select.value = vid;
      updateFlag(select);
    }
    post('/api/orders/' + row.dataset.id, 'POST', { vendor_id: vendor.id }, null);
    rowNote(row, 'vendor from quote: ' + vendor.name);
  }

  function addVendorOption(vendor) {
    var vid = String(vendor.id);
    document.querySelectorAll('.vendor-select').forEach(function (sel) {
      if (!sel.querySelector('option[value="' + vid + '"]')) {
        var opt = document.createElement('option');
        opt.value = vid; opt.textContent = vendor.name;
        if (vendor.incomplete) opt.dataset.incomplete = '1';
        sel.appendChild(opt);
      }
    });
  }

  /* --- vendor helpers ------------------------------------------------ */

  function updateFlag(select) {
    var cell = select.closest(".vendor-cell");
    if (!cell) return;
    var opt = select.selectedOptions[0];
    var flag = cell.querySelector(".flag");
    flag.classList.toggle("on", !!(opt && opt.dataset.incomplete === "1"));
  }

  function hostOf(url) {
    try {
      if (url.indexOf("://") === -1) url = "https://" + url;
      var h = new URL(url).hostname.toLowerCase();
      return h.replace(/^www\./, "");
    } catch (e) { return ""; }
  }

  /* Quote-storage provider check — derived from QUOTE_STORAGE_DOMAINS injected
     by base.html from vendor_catalog.yaml, so no hardcoding needed here. */
  function quoteProvider(host) {
    var domains = (typeof QUOTE_STORAGE_DOMAINS !== 'undefined')
                  ? QUOTE_STORAGE_DOMAINS : [];
    for (var i = 0; i < domains.length; i++) {
      var d = domains[i].toLowerCase();
      if (host === d || host.endsWith('.' + d)) return d;
    }
    return null;
  }

  function rowNote(row, txt, isError) {
    var note = row.querySelector(".row-note");
    if (!note) return;
    note.textContent = txt || "";
    note.classList.toggle("err", !!isError);
  }

  function quoteVendor(linkInput, row) {
    var select = row.querySelector(".vendor-select");
    rowNote(row, "reading quote…");
    post("/api/orders/" + row.dataset.id + "/quote_vendor", "POST",
         { link: linkInput.value.trim() },
         function (data) {
           if (data.price) setCostInput(row, data.price);
           if (data.matched) {
             if (select) {
               var vid = String(data.vendor_id);
               var opt = select.querySelector('option[value="' + vid + '"]');
               if (opt) opt.textContent = data.vendor_name; // refresh if name changed
               select.value = vid;
               updateFlag(select);
             }
             var noteMsg = "vendor from quote: " + data.vendor_name;
             if (data.price) noteMsg += " · $" + data.price;
             rowNote(row, noteMsg);
           } else if (data.fuzzy_candidates !== undefined || data.extracted) {
             rowNote(row, data.price ? ("price: $" + data.price) : "");
             showVendorPopup(row, data);
           } else {
             rowNote(row, data.message || "couldn't read the quote", !data.price);
           }
         });
  }

  /* --- price fetch --------------------------------------------------- */

  function fmtCurrency(s) {
    var f = parseFloat(String(s).replace(/,/g, ''));
    if (isNaN(f)) return s;
    return f.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  function setCostInput(row, price) {
    var inp = row.querySelector('input[data-field="cost"]');
    if (inp && !inp.value) {
      inp.value = fmtCurrency(price);
      saveField(inp);
    }
  }

  function fetchPrice(linkInput) {
    var row = rowOf(linkInput);
    if (!row) return;
    var costInp = row.querySelector('input[data-field="cost"]');
    if (costInp && costInp.value) return;   // don't overwrite a manually-entered cost
    rowNote(row, "fetching price…");
    post("/api/orders/" + row.dataset.id + "/fetch_price", "POST",
         { link: linkInput.value.trim() },
         function (data) {
           if (data.ok && data.price) {
             setCostInput(row, data.price);
             rowNote(row, "price: $" + data.price);
           } else {
             rowNote(row, "price not found — enter manually");
           }
         });
  }

  function linkVendor(linkInput, row) {
    rowNote(row, "looking up vendor…");
    post("/api/orders/" + row.dataset.id + "/link_vendor", "POST",
         { link: linkInput.value.trim() },
         function (data) {
           if (data.matched) {
             var select = row.querySelector(".vendor-select");
             if (select) {
               var vid = String(data.vendor_id);
               var opt = select.querySelector('option[value="' + vid + '"]');
               if (opt) opt.textContent = data.vendor_name;
               select.value = vid;
               updateFlag(select);
             }
             rowNote(row, "vendor: " + data.vendor_name);
           } else if (data.fuzzy_candidates !== undefined || data.extracted) {
             rowNote(row, "");
             showVendorPopup(row, data);
           } else {
             rowNote(row, "");
           }
         });
  }

  function autoVendor(linkInput) {
    var row = rowOf(linkInput);
    if (!row) return;
    var host = hostOf(linkInput.value.trim());
    if (!host) return;
    if (quoteProvider(host)) { quoteVendor(linkInput, row); return; }
    var select = row.querySelector(".vendor-select");

    fetchPrice(linkInput);   // always try to fill price

    if (!select) return;

    // Fast path: domain matches a vendor already in the dropdown.
    // Always show a note, even if that vendor is already selected.
    var localMatch = false;
    for (var i = 0; i < select.options.length; i++) {
      var d = select.options[i].dataset.domain;
      if (d && (host === d || host.endsWith("." + d))) {
        if (!select.value) {
          select.value = select.options[i].value;
          updateFlag(select);
          saveField(select);
        }
        rowNote(row, "vendor: " + select.options[i].textContent.trim());
        localMatch = true;
        break;
      }
    }

    // If vendor was manually chosen for a non-matching URL, leave it alone.
    if (!localMatch && select.value) return;

    // Slow path: ask server to identify vendor from homepage / catalog.
    if (!localMatch) {
      linkVendor(linkInput, row);
    }
  }

  /* --- trackers ------------------------------------------------------ */

  function addChip(cell, email, orderId) {
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = email;
    var x = document.createElement("button");
    x.type = "button"; x.className = "chip-x"; x.dataset.email = email;
    x.textContent = "\u00d7";
    chip.appendChild(x);
    cell.querySelector(".chips").appendChild(chip);
  }

  /* --- delegated events ---------------------------------------------- */

  document.addEventListener("input", function (e) {
    if (e.target.matches("[data-field]")) debounceSave(e.target);
  });

  function updateStatusClass(sel) {
    var val = (sel.value || 'submitted').replace(/\s+/g, '-');
    sel.className = sel.className.replace(/\bstatus-\S+/g, '').trim();
    sel.classList.add('status-select', 'status-' + val);
  }

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches("select[data-field]")) {
      saveField(t);
      if (t.classList.contains("vendor-select")) updateFlag(t);
      if (t.classList.contains("status-select")) updateStatusClass(t);
    } else if (t.matches('input[data-field="link"]')) {
      autoVendor(t);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.classList.contains("tracker-input")) return;
    e.preventDefault();
    var input = e.target;
    var email = input.value.trim();
    if (!email) return;
    var row = rowOf(input);
    post("/api/orders/" + row.dataset.id + "/trackers", "POST",
         { email: email },
         function (data) {
           addChip(input.closest(".tracker-cell"), data.email, row.dataset.id);
           input.value = "";
         });
  });

  document.addEventListener("click", function (e) {
    // chip remove
    if (e.target.classList.contains("chip-x")) {
      var chip = e.target.closest(".chip");
      var row = rowOf(e.target);
      post("/api/orders/" + row.dataset.id + "/trackers", "DELETE",
           { email: e.target.dataset.email },
           function () { chip.remove(); });
      return;
    }
    // delete button: show inline confirm
    if (e.target.classList.contains("del-btn")) {
      var confirm = e.target.closest(".row-del").querySelector(".del-confirm");
      if (confirm) confirm.hidden = false;
      return;
    }
    // cancel delete
    if (e.target.classList.contains("del-no")) {
      var confirm = e.target.closest(".del-confirm");
      if (confirm) confirm.hidden = true;
      return;
    }
    // confirm delete
    if (e.target.classList.contains("del-yes")) {
      var row = rowOf(e.target);
      post("/api/orders/" + row.dataset.id + "/delete", "POST", {},
           function () { row.remove(); });
      return;
    }
  });

  /* initialise flags on load */
  document.querySelectorAll(".vendor-select").forEach(updateFlag);

  /* --- bookmarklet capture polling ---------------------------------------- */

  var _lastCapCheck = 0;

  function checkCaptures() {
    // Only on the orders page; debounce to at most once per 3 s
    if (!document.querySelector(".sheet")) return;
    var now = Date.now();
    if (now - _lastCapCheck < 3000) return;
    _lastCapCheck = now;

    fetch("/api/captures")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.items || !d.items.length) return;
        var remaining = d.items.length;
        d.items.forEach(function (item) {
          fetch("/api/orders/from_capture", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(item)
          }).then(function () {
            remaining--;
            if (remaining === 0) location.reload();
          });
        });
      });
  }

  checkCaptures();
  window.addEventListener("focus", checkCaptures);

  /* --- excel import -------------------------------------------------- */

  var WEB_FIELDS = [
    { key: "description", labels: ["description", "desc", "item", "name", "product"] },
    { key: "link",        labels: ["link", "url", "href", "website"] },
    { key: "vendor",      labels: ["vendor", "supplier", "manufacturer", "company", "store"] },
    { key: "project",     labels: ["project", "grant", "fund", "account", "budget", "code"] },
    { key: "use_note",    labels: ["use", "purpose", "note", "reason", "detail", "comment"] },
    { key: "cost",        labels: ["cost", "price", "amount", "total", "unit price", "each"] },
    { key: "quantity",    labels: ["quantity", "qty", "count", "num", "number", "units"] }
  ];
  var WEB_FIELD_DISPLAY = {
    description: "Description", link: "Link", vendor: "Vendor",
    project: "Project", use_note: "Use", cost: "Cost", quantity: "Qty"
  };

  function xlFuzzyKey(colName) {
    var norm = colName.toLowerCase().replace(/[^a-z0-9]/g, '');
    var best = null, bestScore = 0;
    WEB_FIELDS.forEach(function (f) {
      var score = 0;
      f.labels.forEach(function (lbl) {
        var n = lbl.replace(/[^a-z0-9]/g, '');
        if (norm === n) { score = Math.max(score, 1.0); return; }
        if (norm.indexOf(n) !== -1 || n.indexOf(norm) !== -1) { score = Math.max(score, 0.85); return; }
        // bigram overlap
        function bigrams(s) {
          var bg = {}; for (var i = 0; i < s.length - 1; i++) bg[s.slice(i, i+2)] = 1; return bg;
        }
        var bA = bigrams(norm), bB = bigrams(n);
        var inter = 0;
        Object.keys(bA).forEach(function(k) { if (bB[k]) inter++; });
        var union = Object.keys(bA).length + Object.keys(bB).length - inter;
        if (union > 0) score = Math.max(score, inter / union);
      });
      if (score > bestScore) { bestScore = score; best = f.key; }
    });
    return bestScore >= 0.5 ? best : null;
  }

  var _xlOverlay = null, _xlPopup = null;

  function closeXlModal() {
    if (_xlOverlay) { _xlOverlay.remove(); _xlOverlay = null; }
    if (_xlPopup)   { _xlPopup.remove();   _xlPopup = null; }
  }

  function showXlModal(headers, dataRows) {
    closeXlModal();

    var overlay = document.createElement('div');
    overlay.className = 'xl-overlay';
    overlay.onclick = closeXlModal;
    document.body.appendChild(overlay);
    _xlOverlay = overlay;

    var pop = document.createElement('div');
    pop.className = 'xl-popup';
    pop.onclick = function(e) { e.stopPropagation(); };
    _xlPopup = pop;

    var head = document.createElement('div');
    head.className = 'xl-popup-head';
    var title = document.createElement('strong');
    title.textContent = 'Map spreadsheet columns (' + dataRows.length + ' rows)';
    var xBtn = document.createElement('button');
    xBtn.type = 'button'; xBtn.className = 'vendor-popup-x';
    xBtn.textContent = '×'; xBtn.onclick = closeXlModal;
    head.appendChild(title); head.appendChild(xBtn);
    pop.appendChild(head);

    var hint = document.createElement('p');
    hint.className = 'vendor-popup-label';
    hint.style.marginBottom = '.7rem';
    hint.textContent = 'Choose which spreadsheet column maps to each order field. '
                     + 'Columns with no useful match are set to “Do not import”.';
    pop.appendChild(hint);

    var tbl = document.createElement('table');
    var thead = document.createElement('thead');
    var hr = document.createElement('tr');
    ['Spreadsheet column', 'Maps to'].forEach(function(h) {
      var th = document.createElement('th'); th.textContent = h; hr.appendChild(th);
    });
    thead.appendChild(hr); tbl.appendChild(thead);

    var tbody = document.createElement('tbody');
    var selects = {};   // header → <select> element

    headers.forEach(function(col) {
      var tr = document.createElement('tr');
      var td1 = document.createElement('td'); td1.textContent = col;
      var td2 = document.createElement('td');
      var sel = document.createElement('select');
      var none = document.createElement('option');
      none.value = ''; none.textContent = 'Do not import';
      sel.appendChild(none);
      WEB_FIELDS.forEach(function(f) {
        var opt = document.createElement('option');
        opt.value = f.key;
        opt.textContent = WEB_FIELD_DISPLAY[f.key];
        sel.appendChild(opt);
      });
      var matched = xlFuzzyKey(col);
      sel.value = matched || '';
      td2.appendChild(sel);
      tr.appendChild(td1); tr.appendChild(td2);
      tbody.appendChild(tr);
      selects[col] = sel;
    });
    tbl.appendChild(tbody);
    pop.appendChild(tbl);

    var actions = document.createElement('div');
    actions.className = 'xl-popup-actions';

    var importBtn = document.createElement('button');
    importBtn.type = 'button'; importBtn.className = 'submit-btn';
    importBtn.textContent = 'Import ' + dataRows.length + ' rows';
    importBtn.onclick = function() {
      var rows = dataRows.map(function(raw) {
        var obj = {};
        headers.forEach(function(col) {
          var field = selects[col].value;
          if (field) obj[field] = (raw[col] !== undefined && raw[col] !== null) ? String(raw[col]) : '';
        });
        return obj;
      });
      importBtn.disabled = true; importBtn.textContent = 'Importing…';
      fetch('/api/orders/import_excel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: rows })
      }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) { closeXlModal(); location.reload(); }
        else { importBtn.disabled = false; importBtn.textContent = 'Import ' + dataRows.length + ' rows'; }
      }).catch(function() {
        importBtn.disabled = false; importBtn.textContent = 'Import ' + dataRows.length + ' rows';
      });
    };
    actions.appendChild(importBtn);

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button'; cancelBtn.className = 'mini';
    cancelBtn.textContent = 'Cancel'; cancelBtn.onclick = closeXlModal;
    actions.appendChild(cancelBtn);
    pop.appendChild(actions);
    document.body.appendChild(pop);
  }

  var xlInput = document.getElementById('xl-file-input');
  if (xlInput) {
    xlInput.addEventListener('change', function() {
      var file = xlInput.files[0];
      if (!file) return;
      xlInput.value = '';   // reset so same file can be re-selected
      if (typeof XLSX === 'undefined') {
        alert('SheetJS library not loaded — please refresh the page and try again.');
        return;
      }
      var reader = new FileReader();
      reader.onload = function(e) {
        var wb = XLSX.read(e.target.result, { type: 'array' });
        var ws = wb.Sheets[wb.SheetNames[0]];
        var rows = XLSX.utils.sheet_to_json(ws, { defval: '' });
        if (!rows.length) { alert('Spreadsheet appears to be empty.'); return; }
        var headers = Object.keys(rows[0]);
        showXlModal(headers, rows);
      };
      reader.readAsArrayBuffer(file);
    });
  }

})();
