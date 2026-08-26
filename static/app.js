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

  /* --- totals row (sum of cost * quantity for every row currently
     shown — rows hidden by the column filter are excluded) ---------- */

  function parseMoney(raw) {
    if (raw == null) return null;
    var s = String(raw).replace(/,/g, "").trim();
    if (s === "") return null;
    var n = parseFloat(s);
    return isNaN(n) ? null : n;
  }

  function updateOrderTotals() {
    var totalEl = document.getElementById("sheet-total");
    var msgEl = document.getElementById("sheet-missing");
    var countsEl = document.getElementById("sheet-counts");
    if (!totalEl && !msgEl && !countsEl) return;

    var total = 0;
    var missingQty = [];
    var missingCost = [];
    var n = 0;
    var itemCount = 0;

    document.querySelectorAll(".order-row").forEach(function (row) {
      if (row.classList.contains("filtered-out")) return;
      n++;
      var costEl = row.querySelector('[data-field="cost"]');
      var qtyEl = row.querySelector('[data-field="quantity"]');
      var cost = costEl ? parseMoney(costEl.value) : null;
      var qty = qtyEl ? parseMoney(qtyEl.value) : null;
      if (qty === null) missingQty.push(n);
      if (cost === null) missingCost.push(n);
      if (qty !== null) itemCount += qty;
      if (cost !== null && qty !== null) total += cost * qty;
    });

    if (totalEl) {
      totalEl.textContent = "Total: $" + total.toLocaleString(
        undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    if (msgEl) {
      var parts = [];
      if (missingQty.length) parts.push({ label: "qty", rows: missingQty });
      if (missingCost.length) parts.push({ label: "cost", rows: missingCost });
      msgEl.textContent = parts.length ? "missing " + parts.map(function (p, i) {
        return p.label + " → " + (i === 0 ? "row #" : "") + p.rows.join(",");
      }).join(" ") : "";
    }
    if (countsEl) {
      countsEl.textContent = "Items: " + itemCount.toLocaleString() +
        " · Unique items: " + n.toLocaleString();
    }
  }

  function saveField(input, onOk) {
    var row = rowOf(input);
    if (!row) return;
    var field = input.dataset.field;
    var body = {};
    body[field] = input.value;
    post("/api/orders/" + row.dataset.id, "POST", body, onOk);
  }

  function debounceSave(input) {
    var row = rowOf(input);
    if (!row || !input.dataset.field) return;
    var key = row.dataset.id + ":" + input.dataset.field;
    clearTimeout(timers[key]);
    timers[key] = setTimeout(function () { saveField(input); }, 400);
  }

  function updateOpenLinkVisibility(input) {
    var row = rowOf(input);
    if (!row || !row.classList.contains("submitted-row")) return;
    var cell = input.closest(".link-cell");
    if (!cell) return;
    var link = cell.querySelector(".link-out");
    if (link) link.hidden = !input.value.trim();
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

  /* --- submitted column filters + sorting -----------------------------
     Both are encoded in GET parameters, so views remain bookmarkable. */

  var FILTER_COLUMNS = [
    { field: "submitted_at", type: "text" },
    { field: "user_email",   type: "text" },
    { field: "description",  type: "text" },
    { field: "link",         type: "text" },
    { field: "vendor_id",    type: "checkbox", label: "Vendor" },
    { field: "project_id",   type: "checkbox", label: "Project" },
    { field: "use_note",     type: "text" },
    { field: "cost",         type: "text" },
    { field: "quantity",     type: "text" },
    { field: "order_status", type: "checkbox", label: "Order Status" },
    { field: "invoice_id",   type: "checkbox", label: "Invoice" },
    { field: "trackers",     type: "tracker", label: "Trackers" }
  ];
  var FILTER_FIELD_DISPLAY = {
    submitted_at: "Submitted", user_email: "By", description: "Description",
    link: "Link", use_note: "Use", cost: "Cost", quantity: "Qty",
    trackers: "Trackers"
  };

  var submittedSheet = document.getElementById("submitted-sheet");

  if (submittedSheet) {
    var initialFilterState = {};
    try {
      initialFilterState = JSON.parse(submittedSheet.dataset.filterState || "{}");
    } catch (e) {
      initialFilterState = {};
    }
    var initialFilterChoices = {};
    try {
      initialFilterChoices = JSON.parse(submittedSheet.dataset.filterChoices || "{}");
    } catch (e) {
      initialFilterChoices = {};
    }
    var sortState = [];
    try {
      sortState = JSON.parse(submittedSheet.dataset.sortState || "[]");
    } catch (e) {
      sortState = [];
    }
    var currentUser = (submittedSheet.dataset.currentUser || "").toLowerCase();
    var filterState = {};
    FILTER_COLUMNS.forEach(function (col) {
      var initial = initialFilterState[col.field] || {};
      if (col.type === "checkbox") {
        filterState[col.field] = { selected: new Set(initial.selected || []) };
      } else if (col.type === "tracker") {
        filterState[col.field] = {
          selected: new Set(initial.selected || []),
          mode: initial.mode === "and" ? "and" : "or",
          scope: initial.scope || "default"
        };
      } else {
        filterState[col.field] = { query: initial.query || "", regex: !!initial.regex };
      }
    });

    function pageUrl() {
      var params = new URLSearchParams(window.location.search);
      FILTER_COLUMNS.forEach(function (col) {
        if (col.type === "tracker") {
          params.delete("tracker");
          params.delete("tracker_mode");
          params.delete("filter_trackers");
          params.delete("filter_trackers_regex");
          var trackerState = filterState.trackers;
          var selectedTrackers = Array.from(trackerState.selected);
          var isDefault = selectedTrackers.length === 1 &&
            selectedTrackers[0].toLowerCase() === currentUser && trackerState.mode === "or";
          if (!selectedTrackers.length || trackerState.scope === "all") {
            params.set("tracker", "all");
          } else if (!isDefault) {
            selectedTrackers.forEach(function (email) { params.append("tracker", email); });
            if (trackerState.mode === "and") params.set("tracker_mode", "and");
          }
          return;
        }
        var key = "filter_" + col.field;
        params.delete(key);
        params.delete(key + "_regex");
        var state = filterState[col.field];
        if (col.type === "checkbox") {
          state.selected.forEach(function (value) { params.append(key, value); });
        } else if (state.query !== "") {
          params.set(key, state.query);
          if (state.regex) params.set(key + "_regex", "1");
        }
      });
      params.delete("sort");
      sortState.forEach(function (spec) {
        params.append("sort", spec.field + ":" + spec.direction);
      });
      var query = params.toString();
      return window.location.pathname + (query ? "?" + query : "") + window.location.hash;
    }

    function applyGet() {
      window.location.assign(pageUrl());
    }

    function filterLabel(col) {
      return col.label || FILTER_FIELD_DISPLAY[col.field] || col.field;
    }

    function collectUniqueValues(col) {
      if (col.fixedValues) {
        return col.fixedValues.map(function (v) {
          return { value: v, label: v.charAt(0).toUpperCase() + v.slice(1) };
        });
      }
      return initialFilterChoices[col.field] || [];
    }

    function compileRegexSafe(pattern) {
      try { return { re: new RegExp(pattern, "i"), error: null }; }
      catch (e) { return { re: null, error: e.message }; }
    }

    function filterIsActive(col) {
      var state = filterState[col.field];
      if (col.type === "checkbox") return state.selected.size > 0;
      if (col.type === "tracker") return state.scope !== "all" && state.selected.size > 0;
      return state.query !== "";
    }

    function updateHeaderControls() {
      FILTER_COLUMNS.forEach(function (col) {
        var button = submittedSheet.querySelector(
          '.column-filter[data-filter-field="' + col.field + '"]');
        if (button) button.classList.toggle("active", filterIsActive(col));
      });

      submittedSheet.querySelectorAll(".column-sort").forEach(function (button) {
        button.classList.remove("active");
      });
      submittedSheet.querySelectorAll(".sort-priority").forEach(function (button) {
        button.hidden = true;
        button.textContent = "";
      });
      sortState.forEach(function (spec, index) {
        var arrow = submittedSheet.querySelector(
          '.column-sort[data-sort-field="' + spec.field + '"]' +
          '[data-sort-dir="' + spec.direction + '"]');
        var priority = submittedSheet.querySelector(
          '.sort-priority[data-sort-priority-field="' + spec.field + '"]');
        if (arrow) arrow.classList.add("active");
        if (priority) {
          priority.hidden = false;
          priority.textContent = String(index + 1);
          priority.setAttribute("aria-label", "Sort priority " + (index + 1) +
            "; click to promote it one level");
        }
      });
    }

    var _filterOverlay = null, _filterPopup = null;

    function closeFilterPopup() {
      if (_filterOverlay) { _filterOverlay.remove(); _filterOverlay = null; }
      if (_filterPopup)   { _filterPopup.remove();   _filterPopup = null; }
    }

    function copyState(col) {
      var state = filterState[col.field];
      if (col.type === "checkbox") return { selected: new Set(state.selected) };
      if (col.type === "tracker") return {
        selected: new Set(state.selected), mode: state.mode, scope: state.scope
      };
      return { query: state.query, regex: state.regex };
    }

    function clearedState(col) {
      if (col.type === "checkbox") return { selected: new Set() };
      if (col.type === "tracker") return { selected: new Set(), mode: "or", scope: "all" };
      return { query: "", regex: false };
    }

    function buildCheckboxSection(col, state) {
      var sec = document.createElement("div");
      sec.className = "vendor-popup-section";
      var lbl = document.createElement("div");
      lbl.className = "vendor-popup-label";
      lbl.textContent = filterLabel(col);
      sec.appendChild(lbl);

      var values = collectUniqueValues(col);
      var actions = document.createElement("div");
      actions.className = "filter-section-actions";
      var allBtn = makePopupBtn("Select all", "", function () {
        values.forEach(function (v) { state.selected.add(v.value); });
        sec.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = true; });
      });
      var noneBtn = makePopupBtn("Clear", "", function () {
        state.selected.clear();
        sec.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = false; });
      });
      actions.appendChild(allBtn); actions.appendChild(noneBtn);
      sec.appendChild(actions);

      if (values.length === 0) {
        var none = document.createElement("div");
        none.className = "filter-check-empty";
        none.textContent = "(no values yet)";
        sec.appendChild(none);
      }

      values.forEach(function (v) {
        var row = document.createElement("label");
        row.className = "filter-check" + (v.value === "" ? " filter-check-empty" : "");
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.dataset.filterValue = v.value;
        cb.checked = state.selected.has(v.value);
        cb.addEventListener("change", function () {
          if (cb.checked) state.selected.add(v.value); else state.selected.delete(v.value);
          if (col.field === "order_status" && v.value === "submitted" && cb.checked) {
            state.selected.add("in cart");
            var inCart = sec.querySelector('input[data-filter-value="in cart"]');
            if (inCart) inCart.checked = true;
          }
        });
        row.appendChild(cb);
        row.appendChild(document.createTextNode(" " + v.label));
        sec.appendChild(row);
      });

      if (col.type === "tracker") {
        var andLabel = document.createElement("label");
        andLabel.className = "filter-regex-toggle tracker-and-toggle";
        var andCheckbox = document.createElement("input");
        andCheckbox.type = "checkbox";
        andCheckbox.checked = state.mode === "and";
        andCheckbox.addEventListener("change", function () {
          state.mode = andCheckbox.checked ? "and" : "or";
        });
        andLabel.appendChild(andCheckbox);
        andLabel.appendChild(document.createTextNode(" require every selected tracker (AND)"));
        sec.appendChild(andLabel);
      }

      return sec;
    }

    function buildTextSection(col, state) {
      var sec = document.createElement("div");
      sec.className = "vendor-popup-section";
      var lbl = document.createElement("div");
      lbl.className = "vendor-popup-label";
      lbl.textContent = filterLabel(col);
      sec.appendChild(lbl);

      var textRow = document.createElement("div");
      textRow.className = "filter-text-row";

      var input = document.createElement("input");
      input.type = "text";
      input.className = "filter-text-input";
      input.placeholder = "Search…";
      input.value = state.query;

      var errEl = document.createElement("div");
      errEl.className = "filter-regex-error";
      errEl.hidden = true;

      function runFilter() {
        var c = state.regex && state.query !== ""
          ? compileRegexSafe(state.query) : null;
        if (state.regex && state.query !== "" && c && c.error) {
          errEl.textContent = "Invalid regex: " + c.error;
          errEl.hidden = false;
        } else {
          errEl.hidden = true;
        }
      }

      input.addEventListener("input", function () {
        state.query = input.value;
        runFilter();
      });

      var regexLbl = document.createElement("label");
      regexLbl.className = "filter-regex-toggle";
      var regexCb = document.createElement("input");
      regexCb.type = "checkbox";
      regexCb.checked = state.regex;
      regexCb.addEventListener("change", function () {
        state.regex = regexCb.checked;
        runFilter();
      });
      regexLbl.appendChild(regexCb);
      regexLbl.appendChild(document.createTextNode(" regex"));

      textRow.appendChild(input);
      textRow.appendChild(regexLbl);
      sec.appendChild(textRow);
      sec.appendChild(errEl);

      return sec;
    }

    function showFilterPopup(col) {
      closeFilterPopup();
      var workingState = copyState(col);

      var overlay = document.createElement("div");
      overlay.className = "xl-overlay";
      overlay.onclick = closeFilterPopup;
      document.body.appendChild(overlay);
      _filterOverlay = overlay;

      var pop = document.createElement("div");
      pop.className = "xl-popup filter-popup";
      pop.onclick = function (e) { e.stopPropagation(); };
      _filterPopup = pop;

      var head = document.createElement("div");
      head.className = "xl-popup-head";
      var title = document.createElement("strong");
      title.textContent = "Filter · " + filterLabel(col);
      var xBtn = document.createElement("button");
      xBtn.type = "button"; xBtn.className = "vendor-popup-x";
      xBtn.textContent = "×"; xBtn.onclick = closeFilterPopup;
      head.appendChild(title); head.appendChild(xBtn);
      pop.appendChild(head);

      pop.appendChild(col.type === "text"
        ? buildTextSection(col, workingState)
        : buildCheckboxSection(col, workingState));

      var actions = document.createElement("div");
      actions.className = "xl-popup-actions";
      actions.appendChild(makePopupBtn("Clear this filter", "mini", function () {
        filterState[col.field] = clearedState(col);
        applyGet();
      }));
      actions.appendChild(makePopupBtn("Cancel", "mini", closeFilterPopup));
      actions.appendChild(makePopupBtn("Apply", "submit-btn", function () {
        if (col.type === "tracker") {
          workingState.scope = workingState.selected.size ? "selected" : "all";
        }
        filterState[col.field] = workingState;
        applyGet();
      }));
      pop.appendChild(actions);

      document.body.appendChild(pop);
      var firstInput = pop.querySelector('input[type="text"]');
      if (firstInput) firstInput.focus();
    }

    submittedSheet.addEventListener("click", function (e) {
      var filterButton = e.target.closest(".column-filter");
      if (filterButton) {
        var filterField = filterButton.dataset.filterField;
        var col = FILTER_COLUMNS.find(function (item) { return item.field === filterField; });
        if (col) showFilterPopup(col);
        return;
      }

      var sortButton = e.target.closest(".column-sort");
      if (sortButton) {
        var field = sortButton.dataset.sortField;
        var direction = sortButton.dataset.sortDir;
        var index = sortState.findIndex(function (item) { return item.field === field; });
        if (index >= 0 && sortState[index].direction === direction) {
          sortState.splice(index, 1);
        } else if (index >= 0) {
          sortState[index].direction = direction;
        } else {
          sortState.push({ field: field, direction: direction });
        }
        applyGet();
        return;
      }

      var priorityButton = e.target.closest(".sort-priority");
      if (priorityButton) {
        var priorityField = priorityButton.dataset.sortPriorityField;
        var priorityIndex = sortState.findIndex(function (item) {
          return item.field === priorityField;
        });
        if (priorityIndex > 0) {
          var promoted = sortState[priorityIndex];
          sortState[priorityIndex] = sortState[priorityIndex - 1];
          sortState[priorityIndex - 1] = promoted;
          applyGet();
        }
      }
    });
    updateHeaderControls();
  }

  /* --- submitted-row bulk selection ---------------------------------- */

  var selectionMode = document.getElementById("selection-mode");
  var changeSelectedBtn = document.getElementById("change-selected");
  var _bulkOverlay = null, _bulkPopup = null;

  function selectedOrderIds() {
    return Array.from(document.querySelectorAll(".row-select:checked")).map(
      function (checkbox) { return parseInt(checkbox.value, 10); }
    );
  }

  function updateSelectionControls() {
    if (!changeSelectedBtn) return;
    var count = selectedOrderIds().length;
    changeSelectedBtn.hidden = count === 0;
    changeSelectedBtn.textContent = "Change selected" + (count ? " (" + count + ")" : "");
  }

  function closeBulkPopup() {
    if (_bulkOverlay) { _bulkOverlay.remove(); _bulkOverlay = null; }
    if (_bulkPopup) { _bulkPopup.remove(); _bulkPopup = null; }
  }

  function bulkField(labelText, control) {
    var label = document.createElement("label");
    label.className = "bulk-field";
    var text = document.createElement("span");
    text.className = "bulk-field-label";
    text.textContent = labelText;
    label.appendChild(text);
    label.appendChild(control);
    return label;
  }

  function bulkOption(value, label) {
    var option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function showBulkPopup() {
    var orderIds = selectedOrderIds();
    if (!orderIds.length) return;
    closeBulkPopup();

    var overlay = document.createElement("div");
    overlay.className = "xl-overlay";
    overlay.onclick = closeBulkPopup;
    document.body.appendChild(overlay);
    _bulkOverlay = overlay;

    var pop = document.createElement("div");
    pop.className = "xl-popup bulk-change-popup";
    pop.onclick = function (e) { e.stopPropagation(); };
    _bulkPopup = pop;

    var head = document.createElement("div");
    head.className = "xl-popup-head";
    var title = document.createElement("strong");
    title.textContent = "Change " + orderIds.length + " selected item" +
      (orderIds.length === 1 ? "" : "s");
    var close = document.createElement("button");
    close.type = "button"; close.className = "vendor-popup-x";
    close.textContent = "\u00d7"; close.onclick = closeBulkPopup;
    head.appendChild(title); head.appendChild(close); pop.appendChild(head);

    var hint = document.createElement("p");
    hint.className = "bulk-change-hint";
    hint.textContent = "Choose one or more changes. Unchanged fields will be left alone.";
    pop.appendChild(hint);

    var form = document.createElement("form");
    form.className = "bulk-change-form";

    var project = document.createElement("select");
    project.name = "project_id";
    project.appendChild(bulkOption("__unchanged__", "Do not change"));
    project.appendChild(bulkOption("", "No project"));
    var sourceProject = document.querySelector(
      '.submitted-row select[data-field="project_id"]');
    if (sourceProject) {
      Array.from(sourceProject.options).forEach(function (sourceOption) {
        if (!sourceOption.value) return;
        project.appendChild(bulkOption(sourceOption.value, sourceOption.textContent));
      });
    }
    form.appendChild(bulkField("Project", project));

    var status = document.createElement("select");
    status.name = "order_status";
    status.appendChild(bulkOption("", "Do not change"));
    [
      ["not ready", "Not ready"],
      ["submitted", "Submitted"],
      ["in cart", "In cart"],
      ["received", "Received"],
      ["requires reimbursement", "Requires reimbursement"]
    ].forEach(function (choice) {
      status.appendChild(bulkOption(choice[0], choice[1]));
    });
    form.appendChild(bulkField("Status", status));

    var tracker = document.createElement("input");
    tracker.name = "tracker_email";
    tracker.type = "email";
    tracker.placeholder = "email@example.org (leave blank for no change)";
    form.appendChild(bulkField("Add tracker", tracker));

    var error = document.createElement("div");
    error.className = "bulk-change-error";
    error.setAttribute("role", "alert");
    form.appendChild(error);

    var actions = document.createElement("div");
    actions.className = "xl-popup-actions";
    actions.appendChild(makePopupBtn("Cancel", "mini", closeBulkPopup));
    var apply = makePopupBtn("Apply changes", "submit-btn", function () {});
    apply.type = "submit";
    actions.appendChild(apply);
    form.appendChild(actions);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      error.textContent = "";
      var body = { order_ids: orderIds };
      if (project.value !== "__unchanged__") body.project_id = project.value || null;
      if (status.value) body.order_status = status.value;
      if (tracker.value.trim()) body.tracker_email = tracker.value.trim();
      if (Object.keys(body).length === 1) {
        error.textContent = "Choose at least one change.";
        return;
      }
      if (!tracker.checkValidity()) {
        error.textContent = "Enter a valid tracker email.";
        tracker.focus();
        return;
      }

      apply.disabled = true;
      apply.textContent = "Applying\u2026";
      fetch("/api/orders/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error(data.error || "could not apply changes");
          return data;
        });
      }).then(function () {
        window.location.reload();
      }).catch(function (err) {
        error.textContent = err.message;
        apply.disabled = false;
        apply.textContent = "Apply changes";
      });
    });

    pop.appendChild(form);
    document.body.appendChild(pop);
    project.focus();
  }

  if (selectionMode) {
    selectionMode.addEventListener("change", function () {
      var enabled = selectionMode.checked;
      submittedSheet.classList.toggle("selection-enabled", enabled);
      document.querySelectorAll(".row-select").forEach(function (checkbox) {
        checkbox.hidden = !enabled;
        if (!enabled) {
          checkbox.checked = false;
          checkbox.closest(".submitted-row").classList.remove("selected");
        }
      });
      updateSelectionControls();
    });
    document.querySelectorAll(".row-select").forEach(function (checkbox) {
      checkbox.addEventListener("change", function () {
        checkbox.closest(".submitted-row").classList.toggle("selected", checkbox.checked);
        updateSelectionControls();
      });
    });
  }
  if (changeSelectedBtn) changeSelectedBtn.addEventListener("click", showBulkPopup);

  /* --- in-cart ordering and invoice popup ---------------------------- */

  var markCartOrderedBtn = document.getElementById("mark-cart-ordered");
  function setInCartCount(count) {
    if (!markCartOrderedBtn) return;
    var ids = [];
    document.querySelectorAll(".submitted-row").forEach(function (row) {
      var status = row.querySelector('.status-select[data-field="order_status"]');
      if (status && status.value === "in cart") ids.push(parseInt(row.dataset.id, 10));
    });
    count = ids.length;
    markCartOrderedBtn.dataset.inCartCount = String(count);
    markCartOrderedBtn.dataset.inCartIds = JSON.stringify(ids);
    markCartOrderedBtn.disabled = count === 0;
    markCartOrderedBtn.textContent = "Mark all in cart as ordered" +
      (count ? " (" + count + ")" : "");
  }
  if (markCartOrderedBtn) {
    markCartOrderedBtn.addEventListener("click", function () {
      if (!window.confirm(
        "Mark every in-cart item on your Submitted page as ordered and create one invoice?")) return;
      markCartOrderedBtn.disabled = true;
      markCartOrderedBtn.textContent = "Creating invoice…";
      fetch("/api/invoices/from-cart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          order_ids: JSON.parse(markCartOrderedBtn.dataset.inCartIds || "[]")
        })
      }).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error(data.error || "could not create invoice");
          return data;
        });
      }).then(function (data) {
        showInvoicePopup({
          id: data.invoice_id,
          nickname: data.nickname,
          invoiceUrl: data.invoice_url || "",
          receiptUrl: data.receipt_url || ""
        }, true, function () { window.location.reload(); });
      }).catch(function (err) {
        markCartOrderedBtn.disabled = false;
        markCartOrderedBtn.textContent = "Mark all in cart as ordered";
        window.alert(err.message);
      });
    });
  }

  var _invoiceOverlay = null, _invoicePopup = null, _invoiceAfterClose = null;

  function closeInvoicePopup() {
    if (_invoiceOverlay) { _invoiceOverlay.remove(); _invoiceOverlay = null; }
    if (_invoicePopup) { _invoicePopup.remove(); _invoicePopup = null; }
    var afterClose = _invoiceAfterClose;
    _invoiceAfterClose = null;
    if (afterClose) afterClose();
  }

  function invoiceField(label, value, field, editing) {
    var row = document.createElement("div");
    row.className = "invoice-field";
    var name = document.createElement("span");
    name.className = "invoice-field-label";
    name.textContent = label;
    row.appendChild(name);

    if (editing) {
      var input = document.createElement("input");
      input.type = field === "nickname" ? "text" : "url";
      input.name = field;
      input.value = value || "";
      if (field === "nickname") input.required = true;
      if (field !== "nickname") input.placeholder = "https://www.dropbox.com/…";
      row.appendChild(input);
    } else {
      var shown = document.createElement("span");
      shown.className = "invoice-field-value";
      if (field !== "nickname" && /^https?:\/\//i.test(value || "")) {
        var link = document.createElement("a");
        link.href = value; link.target = "_blank"; link.rel = "noopener";
        link.textContent = value;
        shown.appendChild(link);
      } else {
        shown.textContent = value || "Not added";
        if (!value) shown.classList.add("invoice-empty");
      }
      row.appendChild(shown);
    }
    return row;
  }

  function invoiceDataFromLink(link) {
    return {
      id: link.dataset.invoiceId,
      nickname: link.dataset.nickname,
      invoiceUrl: link.dataset.invoiceUrl,
      receiptUrl: link.dataset.receiptUrl
    };
  }

  function showInvoicePopup(invoice, editing, afterClose) {
    closeInvoicePopup();
    _invoiceAfterClose = afterClose || null;
    var overlay = document.createElement("div");
    overlay.className = "xl-overlay";
    overlay.onclick = closeInvoicePopup;
    document.body.appendChild(overlay);
    _invoiceOverlay = overlay;

    var pop = document.createElement("div");
    pop.className = "xl-popup invoice-popup";
    pop.onclick = function (e) { e.stopPropagation(); };
    _invoicePopup = pop;

    var head = document.createElement("div");
    head.className = "xl-popup-head";
    var title = document.createElement("strong");
    title.textContent = editing ? "Edit invoice" : "Invoice " + invoice.nickname;
    var close = document.createElement("button");
    close.type = "button"; close.className = "vendor-popup-x";
    close.textContent = "×"; close.onclick = closeInvoicePopup;
    head.appendChild(title); head.appendChild(close); pop.appendChild(head);

    var fields = document.createElement(editing ? "form" : "div");
    fields.className = "invoice-fields";
    fields.appendChild(invoiceField("Nickname", invoice.nickname, "nickname", editing));
    fields.appendChild(invoiceField("Invoice", invoice.invoiceUrl, "invoice_url", editing));
    fields.appendChild(invoiceField("Receipt", invoice.receiptUrl, "receipt_url", editing));

    if (editing) {
      var actions = document.createElement("div");
      actions.className = "xl-popup-actions";
      actions.appendChild(makePopupBtn("Cancel", "mini", closeInvoicePopup));
      var save = makePopupBtn("Save", "submit-btn", function () {});
      save.type = "submit";
      actions.appendChild(save);
      fields.appendChild(actions);
      fields.addEventListener("submit", function (e) {
        e.preventDefault();
        var nickname = fields.elements.nickname.value.trim();
        if (!nickname) return;
        save.disabled = true;
        post("/api/invoices/" + invoice.id, "POST", {
          nickname: nickname,
          invoice_url: fields.elements.invoice_url.value.trim(),
          receipt_url: fields.elements.receipt_url.value.trim()
        }, function () { window.location.reload(); });
      });
    }
    pop.appendChild(fields);
    document.body.appendChild(pop);
    if (editing) fields.elements.nickname.focus();
  }

  /* --- delegated events ---------------------------------------------- */

  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-field]")) return;
    debounceSave(e.target);
    if (e.target.matches('[data-field="link"]')) updateOpenLinkVisibility(e.target);
    if (e.target.matches('[data-field="cost"], [data-field="quantity"]')) updateOrderTotals();
  });

  /* Read the live URL field at click time.  The autosave debounce and the
     anchor's server-rendered href may both still contain the previous value. */
  document.addEventListener("click", function (e) {
    var link = e.target.closest(".link-out");
    if (!link) return;
    var cell = link.closest(".link-cell");
    var input = cell && cell.querySelector('input[data-field="link"]');
    if (!input) return;
    var value = input.value.trim();
    if (!value) {
      e.preventDefault();
      return;
    }
    link.setAttribute("href", value);
  });

  function updateStatusClass(sel) {
    var val = (sel.value || 'submitted').replace(/\s+/g, '-');
    sel.className = sel.className.replace(/\bstatus-\S+/g, '').trim();
    sel.classList.add('status-select', 'status-' + val);
  }

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches("select[data-field]")) {
      if (t.classList.contains("status-select")) {
        saveField(t, function (data) {
          setInCartCount(data.in_cart_count);
        });
      } else {
        saveField(t);
      }
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
    var invoiceName = e.target.closest(".invoice-name");
    if (invoiceName) {
      e.preventDefault();
      showInvoicePopup(invoiceDataFromLink(invoiceName), false);
      return;
    }
    if (e.target.classList.contains("invoice-edit")) {
      var invoiceLink = document.querySelector(
        '.invoice-name[data-invoice-id="' + e.target.dataset.invoiceId + '"]');
      if (invoiceLink) showInvoicePopup(invoiceDataFromLink(invoiceLink), true);
      return;
    }
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
           function () { row.remove(); updateOrderTotals(); });
      return;
    }
  });

  // Submitted-page initial totals come from the GET response. Draft totals are
  // still calculated here, and either page recalculates immediately after edits.
  if (!document.querySelector(".submitted-sheet")) updateOrderTotals();

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
                     + 'Columns with no useful match are set to “Do not import”. '
                     + 'Mapping more than one column to the same field joins their values '
                     + 'with a space, in column order.';
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
          if (!field) return;
          var val = (raw[col] !== undefined && raw[col] !== null) ? String(raw[col]) : '';
          if (!val) return;
          obj[field] = obj[field] ? (obj[field] + ' ' + val) : val;
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
