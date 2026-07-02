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

  function showVendorPopup(row, data) {
    _popupRow = row;
    _popupExtracted = data.extracted || null;
    _popupCandidates = data.fuzzy_candidates || [];
    closeVendorPopup();

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

    if (_popupExtracted) {
      var extSec = document.createElement('div');
      extSec.className = 'vendor-popup-section';
      var extLbl = document.createElement('div');
      extLbl.className = 'vendor-popup-label';
      extLbl.textContent = 'Extracted from quote';
      extSec.appendChild(extLbl);
      var ext = _popupExtracted;
      var lines = [
        ext.name,
        ext.street,
        [ext.city, ext.state, ext.zip].filter(Boolean).join(', ') || null,
        ext.phone ? 'Phone: ' + ext.phone : null,
        ext.website ? 'Web: ' + ext.website : null,
      ];
      lines.filter(Boolean).forEach(function (line) {
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

    if (_popupExtracted && _popupExtracted.name) {
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
        if (ext && ext.phone) patch.phone = ext.phone;
        if (ext && ext.website) patch.website = ext.website;
        if (Object.keys(patch).length > 0) {
          post('/api/vendors/' + candidate.id + '/patch', 'PATCH', patch, null);
        }
      }
      assignVendorToRow(row, candidate);
      closeVendorPopup();
    } else if (action === 'create_new' && ext && ext.name) {
      var body = { name: ext.name };
      if (ext.phone) body.phone = ext.phone;
      if (ext.website) body.website = ext.website;
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
    var select = row.querySelector('.vendor-select');
    if (select) {
      if (!select.querySelector('option[value="' + vendor.id + '"]')) {
        var opt = document.createElement('option');
        opt.value = vendor.id; opt.textContent = vendor.name;
        if (vendor.incomplete) opt.dataset.incomplete = '1';
        select.appendChild(opt);
      }
      select.value = vendor.id;
      updateFlag(select);
    }
    post('/api/orders/' + row.dataset.id, 'POST', { vendor_id: vendor.id }, null);
    rowNote(row, 'vendor from quote: ' + vendor.name);
  }

  function addVendorOption(vendor) {
    document.querySelectorAll('.vendor-select').forEach(function (sel) {
      if (!sel.querySelector('option[value="' + vendor.id + '"]')) {
        var opt = document.createElement('option');
        opt.value = vendor.id; opt.textContent = vendor.name;
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

  /* Dropbox and SharePoint/OneDrive links are quotes, not purchase pages —
     the server fetches the PDF and reads the vendor out of it. */
  function quoteProvider(host) {
    if (host === "dropbox.com" || host.endsWith(".dropbox.com") ||
        host.endsWith("dropboxusercontent.com")) return "dropbox";
    if (host.endsWith("sharepoint.com")) return "sharepoint";
    if (host === "1drv.ms" || host.endsWith("onedrive.live.com")) return "onedrive";
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
           if (data.matched) {
             if (select) { select.value = data.vendor_id; updateFlag(select); }
             rowNote(row, "vendor from quote: " + data.vendor_name);
           } else if (data.fuzzy_candidates !== undefined || data.extracted) {
             rowNote(row, "");
             showVendorPopup(row, data);
           } else {
             rowNote(row, data.message || "couldn't read the quote", true);
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
    if (!select || select.value) return;          // never override a choice
    for (var i = 0; i < select.options.length; i++) {
      var d = select.options[i].dataset.domain;
      if (d && (host === d || host.endsWith("." + d))) {
        select.value = select.options[i].value;
        updateFlag(select);
        saveField(select);
        return;
      }
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

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches("select[data-field]")) {
      saveField(t);
      if (t.classList.contains("vendor-select")) updateFlag(t);
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
    if (!e.target.classList.contains("chip-x")) return;
    var chip = e.target.closest(".chip");
    var row = rowOf(e.target);
    post("/api/orders/" + row.dataset.id + "/trackers", "DELETE",
         { email: e.target.dataset.email },
         function () { chip.remove(); });
  });

  /* initialise flags on load */
  document.querySelectorAll(".vendor-select").forEach(updateFlag);
})();
