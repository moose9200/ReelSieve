/* ReelSieve — small vanilla JS: theme toggle, job submit, job polling, host message, settings actions. */
(function () {
  "use strict";

  // ---------- theme ----------
  var root = document.documentElement;
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("lr-theme", next); } catch (e) {}
    });
  }

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  function postJSON(url, body, extraHeaders) {
    var headers = { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrfMeta ? csrfMeta.content : "" };
    Object.keys(extraHeaders || {}).forEach(function (k) { headers[k] = extraHeaders[k]; });
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { detail: t }; }
        if (!r.ok) {
          var msg = (data && (data.detail || data.error || data.message)) || ("Request failed (" + r.status + ")");
          if (typeof msg !== "string") msg = JSON.stringify(msg);
          var err = new Error(msg); err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }
  function newKey() { return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); }
  // Listing photos come through our own server, so the visitor's browser never contacts Airbnb's CDN.
  function imgSrc(u) { return "/img?u=" + encodeURIComponent(u); }
  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status); return r.json();
    });
  }

  // ---------- index: in-app listing search ----------
  var sform = document.getElementById("search-form");
  if (sform) {
    var sbtn = document.getElementById("search-btn"), sstatus = document.getElementById("search-status"), sres = document.getElementById("search-results");
    function esc(t) { var d = document.createElement("div"); d.textContent = t == null ? "" : String(t); return d.innerHTML; }
    function pick(item) {
      var urlField = document.getElementById("url");
      if (urlField) { urlField.value = item.url; urlField.dispatchEvent(new Event("input")); }
      Array.prototype.forEach.call(sres.querySelectorAll(".result"), function (n) { n.classList.toggle("is-selected", n.getAttribute("data-id") === item.id); });
      var card = document.getElementById("reel-card"); if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
      var fs = document.getElementById("form-status"); if (fs) { fs.className = "form-status"; fs.textContent = "Selected: " + (item.name || item.title) + " — press Generate reel."; }
    }
    var lastData = null, sortSel = document.getElementById("sort");
    function priceNum(p) { var m = String(p || "").replace(/,/g, "").match(/([\d.]+)/); return m ? parseFloat(m[1]) : null; }
    function sortItems(items, mode) {
      var a = items.slice();
      var cmp = {
        recommended: function (x, y) { return ((y.reviews || 0) * (y.rating || 0)) - ((x.reviews || 0) * (x.rating || 0)); },
        price_desc: function (x, y) { return (priceNum(y.price) || -1) - (priceNum(x.price) || -1); },
        price_asc: function (x, y) { return (priceNum(x.price) == null ? 1e12 : priceNum(x.price)) - (priceNum(y.price) == null ? 1e12 : priceNum(y.price)); },
        rating: function (x, y) { return (y.rating || 0) - (x.rating || 0) || (y.reviews || 0) - (x.reviews || 0); },
        reviews: function (x, y) { return (y.reviews || 0) - (x.reviews || 0); },
        photos: function (x, y) { return (y.photos || 0) - (x.photos || 0); }
      }[mode] || null;
      if (cmp) a.sort(cmp); return a;
    }
    if (sortSel) sortSel.addEventListener("change", function () { if (lastData) renderResults(lastData); });
    var reelIndex = {};
    getJSON("/api/reels/index").then(function (d) { reelIndex = d || {}; if (lastData) renderResults(lastData); }).catch(function () {});
    function renderResults(data) {
      lastData = data; sres.innerHTML = "";
      var sortWrap = document.getElementById("sort-wrap"); if (sortWrap) sortWrap.classList.toggle("hidden", !(data.items && data.items.length));
      if (data.items && data.items.length && sortSel) data = { items: sortItems(data.items, sortSel.value) };
      if (!data.items || !data.items.length) { sres.innerHTML = '<p class="empty">No listings found for that search. Try a nearby town or different dates.</p>'; return; }
      data.items.forEach(function (it) {
        var el = document.createElement("article"); el.className = "result"; el.setAttribute("data-id", it.id);
        var rating = it.rating != null ? "★ " + it.rating + (it.reviews != null ? " (" + it.reviews + ")" : "") : "New";
        el.innerHTML = (it.photo ? '<img loading="lazy" src="' + esc(imgSrc(it.photo + "?im_w=720")) + '" alt="">' : "") +
          '<div class="rb"><div class="rn">' + esc(it.name || it.title) + "</div>" +
          '<div class="rt">' + esc(it.title) + "</div>" + (it.summary ? '<div class="rs">' + esc(it.summary) + "</div>" : "") +
          (it.badges && it.badges.length ? '<div><span class="badge">' + esc(it.badges[0]) + "</span></div>" : "") +
          (reelIndex[it.id] && reelIndex[it.id].length ? '<div><a class="badge badge-reel" href="/reels#listing-' + esc(it.id) + '">Reel ready · ' + reelIndex[it.id].length + '</a></div>' : "") +
          '<div class="rm"><span>' + esc(rating) + "</span><b>" + esc(it.price) + (it.price_qualifier ? " " + esc(it.price_qualifier) : "") + "</b></div>" +
          '<div class="rs">' + esc(it.photos) + " photos</div>" +
          '<div class="rbtns"><button type="button" class="btn btn-primary use-btn">Use this listing</button>' +
          '<a class="btn btn-secondary open-link" href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer" title="Open this listing on Airbnb in a new tab">Open on Airbnb ↗</a></div></div>';
        el.querySelector(".use-btn").addEventListener("click", function () { pick(it); });
        el.querySelector("img") && el.querySelector("img").addEventListener("click", function () { pick(it); });
        el.querySelector(".rn").addEventListener("click", function () { window.open(it.url, "_blank", "noopener"); });
        sres.appendChild(el);
      });
    }
    // location autocomplete (Photon via /api/places), debounced, keyboard-navigable
    var locIn = document.getElementById("s-location"), sug = document.getElementById("s-suggest");
    if (locIn && sug) {
      var acTimer = null, acItems = [], acIdx = -1, lastQ = "";
      function hideAc() { sug.classList.add("hidden"); sug.innerHTML = ""; acIdx = -1; locIn.setAttribute("aria-expanded", "false"); }
      function chooseAc(i) { var it = acItems[i]; if (!it) return; locIn.value = it.value; hideAc(); }
      function showAc(items) {
        acItems = items || []; sug.innerHTML = "";
        if (!acItems.length) { hideAc(); return; }
        acItems.forEach(function (it, i) {
          var li = document.createElement("li"); li.setAttribute("role", "option"); li.setAttribute("data-i", i);
          var main = it.value.split(",")[0]; li.innerHTML = "<span>" + esc(main) + "</span><small>" + esc(it.label) + "</small>";
          li.addEventListener("mousedown", function (e) { e.preventDefault(); chooseAc(i); });
          sug.appendChild(li);
        });
        sug.classList.remove("hidden"); locIn.setAttribute("aria-expanded", "true"); acIdx = -1;
      }
      locIn.addEventListener("input", function () {
        var q = locIn.value.trim(); clearTimeout(acTimer);
        if (q.length < 2) { hideAc(); return; }
        acTimer = setTimeout(function () {
          lastQ = q;
          getJSON("/api/places?q=" + encodeURIComponent(q)).then(function (d) { if (locIn.value.trim() === lastQ) showAc(d.items); }).catch(function () { hideAc(); });
        }, 220);
      });
      locIn.addEventListener("keydown", function (e) {
        if (sug.classList.contains("hidden")) return;
        var lis = sug.querySelectorAll("li");
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault(); acIdx = e.key === "ArrowDown" ? Math.min(acIdx + 1, lis.length - 1) : Math.max(acIdx - 1, 0);
          Array.prototype.forEach.call(lis, function (li, i) { li.classList.toggle("is-active", i === acIdx); });
        } else if (e.key === "Enter" && acIdx >= 0) { e.preventDefault(); chooseAc(acIdx); }
        else if (e.key === "Escape") { hideAc(); }
      });
      locIn.addEventListener("blur", function () { setTimeout(hideAc, 150); });
    }
    // date range picker (single popover, click start then end, presets, nights)
    var dpTrig = document.getElementById("s-dates"), dpPop = document.getElementById("dp-pop");
    if (dpTrig && dpPop) {
      var ci = document.getElementById("s-checkin"), co = document.getElementById("s-checkout"), dpText = document.getElementById("dp-text"),
          dpNights = document.getElementById("dp-nights"), dpMonths = document.getElementById("dp-months"), dpSum = document.getElementById("dp-summary");
      var today = new Date(); today.setHours(0, 0, 0, 0);
      var view = new Date(today.getFullYear(), today.getMonth(), 1), start = null, end = null, hover = null;
      var MON = ["January","February","March","April","May","June","July","August","September","October","November","December"], SH = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
      function iso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
      function fmt(d) { return d.getDate() + " " + SH[d.getMonth()]; }
      function days(a, b) { return Math.round((b - a) / 86400000); }
      function same(a, b) { return a && b && a.getTime() === b.getTime(); }
      function renderField() {
        if (start && end) { dpText.textContent = fmt(start) + " – " + fmt(end); dpText.classList.remove("is-empty"); var n = days(start, end); dpNights.textContent = n + (n === 1 ? " night" : " nights"); ci.value = iso(start); co.value = iso(end); }
        else if (start) { dpText.textContent = fmt(start) + " – ?"; dpText.classList.remove("is-empty"); dpNights.textContent = ""; ci.value = iso(start); co.value = ""; }
        else { dpText.textContent = "Add dates"; dpText.classList.add("is-empty"); dpNights.textContent = ""; ci.value = ""; co.value = ""; }
        dpSum.textContent = start && end ? fmt(start) + " to " + fmt(end) + " · " + days(start, end) + " nights" : start ? "Now pick a check-out date" : "Select a check-in date";
      }
      function monthEl(y, m, second) {
        var wrap = document.createElement("div"); wrap.className = "dp-month" + (second ? " second" : "");
        var head = document.createElement("div"); head.className = "dp-mhead";
        var prev = document.createElement("button"); prev.type = "button"; prev.className = "dp-nav"; prev.textContent = "‹"; prev.setAttribute("aria-label", "Previous month"); prev.style.visibility = second ? "hidden" : "visible";
        prev.disabled = (y === today.getFullYear() && m === today.getMonth());
        var next = document.createElement("button"); next.type = "button"; next.className = "dp-nav"; next.textContent = "›"; next.setAttribute("aria-label", "Next month"); next.style.visibility = second || window.innerWidth <= 720 ? "visible" : "hidden";
        if (second) next.style.visibility = "visible";
        var title = document.createElement("span"); title.textContent = MON[m] + " " + y;
        head.appendChild(prev); head.appendChild(title); head.appendChild(next); wrap.appendChild(head);
        prev.addEventListener("click", function () { view = new Date(view.getFullYear(), view.getMonth() - 1, 1); renderMonths(); });
        next.addEventListener("click", function () { view = new Date(view.getFullYear(), view.getMonth() + 1, 1); renderMonths(); });
        var grid = document.createElement("div"); grid.className = "dp-grid";
        ["Mo","Tu","We","Th","Fr","Sa","Su"].forEach(function (d) { var e = document.createElement("div"); e.className = "dp-dow"; e.textContent = d; grid.appendChild(e); });
        var first = new Date(y, m, 1), lead = (first.getDay() + 6) % 7, count = new Date(y, m + 1, 0).getDate();
        for (var i = 0; i < lead; i++) { var e0 = document.createElement("button"); e0.type = "button"; e0.className = "dp-day empty"; e0.tabIndex = -1; grid.appendChild(e0); }
        for (var d = 1; d <= count; d++) {
          (function (d) {
            var date = new Date(y, m, d), b = document.createElement("button"); b.type = "button"; b.className = "dp-day"; b.textContent = d; b.setAttribute("aria-label", fmt(date) + " " + y);
            if (date < today) b.disabled = true;
            if (same(date, today)) b.classList.add("is-today");
            var s2 = start, e2 = end || (start && hover && hover > start ? hover : null);
            if (s2 && same(date, s2)) b.classList.add("is-start");
            if (e2 && same(date, e2)) b.classList.add("is-end");
            if (s2 && e2 && date > s2 && date < e2) b.classList.add("in-range");
            b.addEventListener("click", function () {
              if (!start || (start && end)) { start = date; end = null; }
              else if (date <= start) { start = date; end = null; }
              else { end = date; }
              renderField(); renderMonths();
              if (start && end) setTimeout(closeDp, 250);
            });
            b.addEventListener("mouseenter", function () { if (start && !end) { hover = date; paintHover(); } });
            grid.appendChild(b);
          })(d);
        }
        wrap.appendChild(grid); return wrap;
      }
      function paintHover() {
        var s2 = start, e2 = hover; if (!s2 || !e2 || e2 <= s2) return;
        Array.prototype.forEach.call(dpMonths.querySelectorAll(".dp-day:not(.empty)"), function (b) {
          var lab = b.getAttribute("aria-label"); if (!lab) return; b.classList.remove("in-range", "is-end");
          var parts = lab.split(" "), dt = new Date(parts[2], SH.indexOf(parts[1]), parseInt(parts[0], 10));
          if (dt > s2 && dt < e2) b.classList.add("in-range"); if (same(dt, e2)) b.classList.add("is-end");
        });
      }
      function renderMonths() {
        dpMonths.innerHTML = ""; dpMonths.appendChild(monthEl(view.getFullYear(), view.getMonth(), false));
        var v2 = new Date(view.getFullYear(), view.getMonth() + 1, 1); dpMonths.appendChild(monthEl(v2.getFullYear(), v2.getMonth(), true));
      }
      function openDp() { renderMonths(); renderField(); dpPop.classList.remove("hidden"); dpTrig.setAttribute("aria-expanded", "true"); }
      function closeDp() { dpPop.classList.add("hidden"); dpTrig.setAttribute("aria-expanded", "false"); }
      dpTrig.addEventListener("click", function () { dpPop.classList.contains("hidden") ? openDp() : closeDp(); });
      document.getElementById("dp-done").addEventListener("click", closeDp);
      document.getElementById("dp-clear").addEventListener("click", function () { start = end = hover = null; renderField(); renderMonths(); });
      document.addEventListener("click", function (e) { var path = e.composedPath ? e.composedPath() : []; if (!dpPop.classList.contains("hidden") && path.indexOf(dpPop) === -1 && path.indexOf(dpTrig) === -1) closeDp(); });
      document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDp(); });
      function nextDow(from, dow, minDays) { var d = new Date(from); d.setDate(d.getDate() + (minDays || 0)); while (d.getDay() !== dow) d.setDate(d.getDate() + 1); return d; }
      document.getElementById("dp-presets").addEventListener("click", function (e) {
        var p = e.target.getAttribute("data-preset"); if (!p) return;
        var fri = nextDow(today, 5, 0); if (p === "nextweekend") fri = nextDow(fri, 5, 1);
        if (p === "week") { start = nextDow(today, 6, 0); end = new Date(start); end.setDate(end.getDate() + 7); }
        else { start = fri; end = new Date(fri); end.setDate(end.getDate() + 2); }
        view = new Date(start.getFullYear(), start.getMonth(), 1); renderField(); renderMonths();
      });
      renderField();
    }
    var lmWrap = document.getElementById("load-more-wrap"), lmBtn = document.getElementById("load-more-btn"), lmStatus = document.getElementById("load-more-status");
    var lastQuery = "", nextPage = 1, pagesTotal = 1;
    function updateLoadMore() {
      if (!lmWrap) return; var have = lastData && lastData.items ? lastData.items.length : 0;
      lmWrap.classList.toggle("hidden", !(have && nextPage < pagesTotal));
      if (lmStatus) lmStatus.textContent = have ? have + " listings shown" + (nextPage < pagesTotal ? " · more available" : " · that's all Airbnb returns") : "";
    }
    if (lmBtn) lmBtn.addEventListener("click", function () {
      lmBtn.disabled = true; lmStatus.textContent = "Loading page " + (nextPage + 1) + "…";
      getJSON("/api/search/more?" + lastQuery + "&page=" + nextPage).then(function (d) {
        var ids = {}; lastData.items.forEach(function (i) { ids[i.id] = 1; });
        (d.items || []).forEach(function (i) { if (!ids[i.id]) { ids[i.id] = 1; lastData.items.push(i); } });
        nextPage += 1; pagesTotal = d.pages_total || pagesTotal; renderResults(lastData); sstatus.textContent = lastData.items.length + " listings";
      }).catch(function (err) { lmStatus.textContent = err.message || "Load more failed."; }).then(function () { lmBtn.disabled = false; updateLoadMore(); });
    });
    sform.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var loc = sform.location.value.trim();
      sstatus.className = "form-status";
      if (!loc) { sstatus.textContent = "Enter a location."; sstatus.classList.add("is-error"); sform.location.focus(); return; }
      var q = "location=" + encodeURIComponent(loc) + "&checkin=" + encodeURIComponent(sform.checkin.value || "") + "&checkout=" + encodeURIComponent(sform.checkout.value || "") + "&adults=" + encodeURIComponent(sform.adults.value || "2");
      sbtn.disabled = true; sstatus.textContent = "Searching Airbnb…"; sres.innerHTML = "";
      lastQuery = q;
      getJSON("/api/search?" + q + "&pages=3").then(function (data) { nextPage = data.pages_loaded || 1; pagesTotal = data.pages_total || 1; renderResults(data); sstatus.textContent = data.count + " listings"; updateLoadMore(); })
        .catch(function (err) { sstatus.textContent = err.message || "Search failed."; sstatus.classList.add("is-error"); })
        .then(function () { sbtn.disabled = false; });
    });
  }

  // ---------- index: submit job ----------
  var form = document.getElementById("reel-form");
  if (form) {
    var btn = document.getElementById("submit-btn");
    var status = document.getElementById("form-status");
    var idemKey = null;  // one key per intended reel: a retried or double-clicked submit never charges twice
    form.addEventListener("input", function () { idemKey = null; });
    form.addEventListener("change", function () { idemKey = null; });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var url = form.url.value.trim();
      var aiMotion = !!(form.ai_motion && form.ai_motion.checked && !form.ai_motion.disabled);
      var sendToHost = !!(form.send_to_host && form.send_to_host.checked);
      var message = form.message ? form.message.value : "";
      var style = form.style ? form.style.value : "cinematic";
      status.className = "form-status";
      if (!url) { status.textContent = "Paste an Airbnb listing URL."; status.classList.add("is-error"); form.url.focus(); return; }
      btn.disabled = true;
      status.textContent = "Starting…";
      if (!idemKey) idemKey = newKey();
      postJSON("/api/jobs", { url: url, send_to_host: sendToHost, message: message, ai_motion: aiMotion, style: style, ai_resolution: (form.ai_resolution ? form.ai_resolution.value : "1080p") }, { "Idempotency-Key": idemKey })
        .then(function (data) {
          var id = data && (data.id || data.job_id || (data.job && data.job.id));
          if (!id) throw new Error("Server did not return a job id.");
          window.location.href = "/jobs/" + encodeURIComponent(id);
        })
        .catch(function (err) {
          btn.disabled = false;
          status.textContent = err.message || "Something went wrong.";
          status.classList.add("is-error");
          if (err.status === 402) {  // out of videos: the way forward is the plans page
            var up = document.createElement("a"); up.href = "/upgrade"; up.textContent = "See plans";
            status.appendChild(document.createTextNode(" ")); status.appendChild(up);
          }
        });
    });
    var aiLabel = form.querySelector("label.check.is-disabled"), aiHint = document.getElementById("ai-upgrade");
    if (aiLabel && aiHint) aiLabel.addEventListener("click", function () { aiHint.classList.add("hint-warn"); });
  }

  // ---------- index: listing link or your own photos ----------
  var modeBtns = document.querySelectorAll(".mode-btn[data-mode]");
  function setMode(mode) {
    var photosMode = mode === "photos";
    Array.prototype.forEach.call(modeBtns, function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-mode") === mode)); });
    var lf = document.getElementById("reel-form"), pf = document.getElementById("photos-form"), sc = document.getElementById("search-card");
    if (lf) lf.classList.toggle("hidden", photosMode);
    if (pf) pf.classList.toggle("hidden", !photosMode);
    if (sc) sc.classList.toggle("hidden", photosMode);
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-mode]"), function (b) {
    b.addEventListener("click", function (ev) { ev.preventDefault(); setMode(b.getAttribute("data-mode")); var t = document.getElementById("mode-" + b.getAttribute("data-mode")); if (t) t.focus(); });
  });

  var pform = document.getElementById("photos-form");
  if (pform) {
    var ROOMS = [["auto", "Room: auto"], ["exterior", "Outside / entrance"], ["living", "Living room"], ["kitchen", "Kitchen or dining"],
                 ["bedroom", "Bedroom"], ["bathroom", "Bathroom"], ["garden", "Garden, patio or balcony"], ["spa", "Hot tub, pool or sauna"],
                 ["view", "View"], ["other", "Other"]];
    var OK_TYPES = { "image/jpeg": 1, "image/png": 1, "image/webp": 1 };
    var MIN = +pform.getAttribute("data-min"), MAX = +pform.getAttribute("data-max"),
        MAX_BYTES = +pform.getAttribute("data-max-bytes"), MAX_TOTAL = +pform.getAttribute("data-max-total");
    var picker = document.getElementById("photo-files"), drop = document.getElementById("dropzone"), thumbs = document.getElementById("thumbs"),
        countEl = document.getElementById("photo-count"), pstatus = document.getElementById("photos-status"), pbtn = document.getElementById("photos-submit"),
        prog = document.getElementById("upload-progress"), pfill = document.getElementById("upload-fill"), pbar = document.getElementById("upload-bar"),
        roomsHint = document.getElementById("rooms-hint");
    var chosen = [], pKey = null;  // [{file, room, url}] in upload order; one idempotency key per intended reel
    function mb(n) { return (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB"; }
    function say(el, msg, bad) { el.className = "form-status" + (bad ? " is-error" : ""); el.textContent = msg || ""; }
    function totalBytes() { return chosen.reduce(function (s, c) { return s + c.file.size; }, 0); }
    function renderChosen() {
      thumbs.innerHTML = "";
      chosen.forEach(function (c, i) {
        var li = document.createElement("li"); li.className = "thumb";
        var img = document.createElement("img"); img.src = c.url; img.alt = "Photo " + (i + 1); img.loading = "lazy"; li.appendChild(img);
        var sel = document.createElement("select"); sel.setAttribute("aria-label", "What photo " + (i + 1) + " shows");
        ROOMS.forEach(function (r) { var o = document.createElement("option"); o.value = r[0]; o.textContent = r[1]; if (r[0] === c.room) o.selected = true; sel.appendChild(o); });
        sel.addEventListener("change", function () { c.room = sel.value; pKey = null; });
        var rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-secondary btn-sm thumb-remove"; rm.textContent = "Remove";
        rm.setAttribute("aria-label", "Remove photo " + (i + 1));
        rm.addEventListener("click", function () { URL.revokeObjectURL(c.url); chosen.splice(i, 1); pKey = null; renderChosen(); });
        li.appendChild(sel); li.appendChild(rm); thumbs.appendChild(li);
      });
      if (roomsHint) roomsHint.classList.toggle("hidden", !chosen.length);
      var n = chosen.length, t = totalBytes();
      if (!n) { say(countEl, ""); return; }
      var msg = n + " photo" + (n === 1 ? "" : "s") + " · " + mb(t);
      if (n < MIN) msg += " · add at least " + (MIN - n) + " more";
      else if (n > MAX) msg += " · remove " + (n - MAX) + " to stay within " + MAX;
      else if (t > MAX_TOTAL) msg += " · over the 250 MB total";
      say(countEl, msg, n < MIN || n > MAX || t > MAX_TOTAL);
    }
    function addFiles(list) {
      var refused = [];
      Array.prototype.forEach.call(list || [], function (f) {
        if (!OK_TYPES[f.type]) { refused.push(f.name + " is not a JPEG, PNG or WebP image"); return; }
        if (f.size > MAX_BYTES) { refused.push(f.name + " is larger than 15 MB"); return; }
        chosen.push({ file: f, room: "auto", url: URL.createObjectURL(f) });
      });
      pKey = null; renderChosen();
      say(pstatus, refused.length ? refused.slice(0, 3).join(". ") + (refused.length > 3 ? " (and " + (refused.length - 3) + " more)" : "") + "." : "", refused.length > 0);
    }
    picker.addEventListener("change", function () { addFiles(picker.files); picker.value = ""; });
    ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("is-over"); }); });
    ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("is-over"); }); });
    drop.addEventListener("drop", function (e) { addFiles(e.dataTransfer && e.dataTransfer.files); });
    pform.addEventListener("input", function () { pKey = null; });
    pform.addEventListener("change", function () { pKey = null; });
    function setProgress(p) { var v = Math.round(p); pfill.style.width = v + "%"; pbar.setAttribute("aria-valuenow", v); }
    pform.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = pform.elements;
      var n = chosen.length, title = f.title.value.trim(), where = f.location.value.trim();
      var problem = n < MIN || n > MAX ? "Choose " + MIN + " to " + MAX + " photos (you have " + n + ")." :
                    totalBytes() > MAX_TOTAL ? "Your photos add up to more than 250 MB. Choose fewer or smaller photos." :
                    !title ? "Enter a property title." : !where ? "Enter the location." : null;
      var quotes = pform.querySelectorAll('textarea[name="quote_text"]'), stars = pform.querySelectorAll('select[name="quote_stars"]');
      var anyQuote = false;
      Array.prototype.forEach.call(quotes, function (q, i) {
        var len = q.value.trim().length;
        if (!len) return;
        anyQuote = true;
        if (!problem && (len < 20 || len > 300)) problem = "Each guest quote needs 20 to 300 characters.";
        if (!problem && !(stars[i] && stars[i].value)) problem = "Choose the stars the guest gave for each quote.";
      });
      var real = document.getElementById("quotes_real");
      if (!problem && anyQuote && !(real && real.checked)) problem = "Tick the box to confirm the guest quotes are real reviews, quoted word for word.";
      if (problem) { say(pstatus, problem, true); return; }
      var fd = new FormData();
      chosen.forEach(function (c) { fd.append("photos", c.file, c.file.name); fd.append("room", c.room); });
      ["title", "location", "highlights", "style"].forEach(function (k) { fd.append(k, f[k].value); });
      Array.prototype.forEach.call(quotes, function (q) { fd.append("quote_text", q.value); });
      Array.prototype.forEach.call(stars, function (s) { fd.append("quote_stars", s.value); });
      fd.append("quotes_real", String(!!(real && real.checked)));
      var ai = document.getElementById("p-ai_motion"), res = document.getElementById("ai_resolution");
      fd.append("ai_motion", String(!!(ai && ai.checked && !ai.disabled)));
      fd.append("ai_resolution", res ? res.value : "1080p");
      fd.append("delete_inputs", String(document.getElementById("delete_inputs").checked));
      if (!pKey) pKey = newKey();
      pbtn.disabled = true; prog.classList.remove("hidden"); setProgress(0);
      say(pstatus, "Uploading your photos… 0%");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs/photos");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("X-CSRF-Token", csrfMeta ? csrfMeta.content : "");
      xhr.setRequestHeader("Idempotency-Key", pKey);
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        var p = 90 * e.loaded / e.total; setProgress(p); say(pstatus, "Uploading your photos… " + Math.round(100 * e.loaded / e.total) + "%");
      };
      xhr.upload.onload = function () { setProgress(92); say(pstatus, "Checking your photos, removing location data and saving them to your Google Drive…"); };
      function fail(msg, code) {
        pbtn.disabled = false; prog.classList.add("hidden"); say(pstatus, msg, true);
        if (code === 402) { var up = document.createElement("a"); up.href = "/upgrade"; up.textContent = "See plans"; pstatus.appendChild(document.createTextNode(" ")); pstatus.appendChild(up); }
      }
      xhr.onload = function () {
        var data = null; try { data = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status === 200 && data && data.id) { setProgress(100); say(pstatus, "Starting your reel…"); window.location.href = "/jobs/" + encodeURIComponent(data.id); return; }
        fail((data && typeof data.detail === "string" && data.detail) || "The upload failed (" + xhr.status + "). Try again.", xhr.status);
      };
      xhr.onerror = function () { fail("The upload stopped. Check your connection and try again."); };
      xhr.send(fd);
    });
  }

  // ---------- job: poll, delivery, sharing, cancel, host message ----------
  var jobEl = document.getElementById("job");
  if (jobEl) {
    var jobId = jobEl.getAttribute("data-job-id");
    var el = function (id) { return document.getElementById(id); };
    var pill = el("status-pill"), step = el("step"), pct = el("pct"), fill = el("progress-fill"),
        bar = el("progress"), errEl = el("error"), log = el("log"), videoCard = el("video-card"), hostCard = el("host-card"),
        video = el("video"), dl = el("download-btn"), hostPill = el("host-pill"), reelLink = el("reel-link"),
        hostMsg = el("host-message"), hostStatus = el("host-status"), contactLink = el("contact-link"),
        title = el("listing-title"), loc = el("listing-location"), meta = el("listing-meta");
    var timer = null, lastLogLen = -1, msgTouched = false, extraPolls = 0;
    var jobUrl = "/api/jobs/" + encodeURIComponent(jobId);
    if (hostMsg) hostMsg.addEventListener("input", function () { msgTouched = true; });

    function setPill(node, status) { node.className = "pill pill-" + status; node.textContent = status; }
    function hostText(job) {
      var s = job.host_status || "";
      if (s === "sent") return "Sent to host";
      if (s === "draft") return "Opened in Airbnb — paste and press Send";
      return s || "—";
    }
    function renderDelivery(job) {
      var dp = el("drive-pill"), dl2 = el("drive-link"), box = el("share-box");
      if (dp) {
        dp.className = "pill pill-email pill-" + (job.drive_link ? "done" : "skipped");
        dp.textContent = job.drive_link ? "Delivered · " + (job.shared ? "shared by link" : "private") : "—";
      }
      if (dl2) { dl2.classList.toggle("hidden", !job.drive_link); if (job.drive_link) dl2.href = job.drive_link; }
      if (dl) { dl.classList.toggle("hidden", !job.download_url); if (job.download_url) dl.href = job.download_url; }
      if (box) box.classList.toggle("hidden", !job.drive_link);
      if (reelLink) reelLink.value = job.reel_link || "";
      var cb = el("copy-link-btn"); if (cb) cb.disabled = !job.reel_link;
      var sb = el("share-btn"), ub = el("unshare-btn");
      if (sb) sb.classList.toggle("hidden", !!job.shared);
      if (ub) ub.classList.toggle("hidden", !job.shared);
      if (video && job.stream_url && video.getAttribute("src") !== job.stream_url) { video.src = job.stream_url; video.load(); }
    }
    function renderHost(job) {
      if (hostPill) {
        hostPill.setAttribute("data-status", job.host_status || "");
        hostPill.className = "pill pill-email pill-" + (job.host_status === "sent" || job.host_status === "draft" ? "done" : "skipped");
        hostPill.textContent = hostText(job);
      }
      if (hostMsg && !msgTouched && (job.message_final || job.message)) hostMsg.value = job.message_final || job.message;
      var yt = el("yt-title"); if (yt && job.youtube_title) yt.value = job.youtube_title;
      if (contactLink && job.contact_url) contactLink.href = job.contact_url;
      if (hostStatus && job.host_error && !hostStatus.textContent) hostStatus.textContent = job.host_error;
    }

    function render(job) {
      var st = job.status || "queued";
      jobEl.setAttribute("data-status", st);
      setPill(pill, st);
      step.textContent = job.step || "";
      var p = Math.max(0, Math.min(100, Number(job.progress) || 0));
      pct.textContent = p + "%"; fill.style.width = p + "%"; bar.setAttribute("aria-valuenow", p);
      var cr = el("cancel-row"); if (cr) cr.classList.toggle("hidden", !job.cancellable && !job.cancel_requested);
      var cbtn = el("cancel-btn"); if (cbtn) cbtn.disabled = !job.cancellable;

      if (job.listing) {
        if (job.listing.title && title.textContent !== job.listing.title) title.textContent = job.listing.title;
        if (job.listing.location && loc) loc.textContent = job.listing.location;
        if (job.listing.rating && !el("listing-rating")) {
          var r = document.createElement("span"); r.id = "listing-rating";
          r.textContent = "★ " + job.listing.rating + (job.listing.count ? " (" + job.listing.count + ")" : "");
          var d = document.createElement("span"); d.className = "dot"; d.textContent = "·";
          meta.insertBefore(r, loc.nextSibling); meta.insertBefore(d, r);
        }
      }
      if (job.error) { errEl.textContent = job.error; errEl.classList.remove("hidden"); } else { errEl.classList.add("hidden"); }

      var lines = Array.isArray(job.log) ? job.log : [];
      if (lines.length !== lastLogLen) {
        var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
        log.textContent = lines.join("\n") + (lines.length ? "\n" : "");
        if (atBottom) log.scrollTop = log.scrollHeight;
        lastLogLen = lines.length;
      }
      if (st === "done") {
        renderDelivery(job);
        videoCard.classList.remove("hidden");
        if (hostCard) hostCard.classList.remove("hidden");
        renderHost(job);
      }
      var inp = el("inputs-state");
      if (inp && job.inputs === "deleted") inp.textContent = "Your uploaded photos were deleted from your Google Drive, as you asked.";
      if (inp && job.inputs === "delete_failed") inp.textContent = "We could not delete your uploaded photos from Google Drive. They are in the Inputs folder inside your ReelSieve folder; delete them there if you like.";
      // a photo reel's Drive clean-up lands just after it finishes: keep polling briefly until it is reported
      var terminal = st === "done" || st === "failed" || st === "cancelled";
      var cleaning = terminal && job.source === "photos" && job.delete_inputs && !job.inputs && extraPolls++ < 15;
      if (terminal && !cleaning) stop();
    }
    function poll() {
      getJSON(jobUrl).then(render).catch(function () {});
    }
    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    var initial = jobEl.getAttribute("data-status");
    if (initial !== "done" && initial !== "failed" && initial !== "cancelled") {
      poll(); timer = setInterval(poll, 2000);
      document.addEventListener("visibilitychange", function () { if (!document.hidden && timer) poll(); });
    } else { poll(); }

    var cancelBtn = el("cancel-btn");
    if (cancelBtn) cancelBtn.addEventListener("click", function () {
      var out = el("cancel-status"); cancelBtn.disabled = true; out.className = "form-status"; out.textContent = "Stopping…";
      postJSON(jobUrl + "/cancel").then(function (job) {
        render(job);
        out.textContent = job.status === "cancelled" ? "Cancelled — nothing was charged." : (job.status === "uploading" || job.status === "done") ? "Already delivering to your Drive — it can no longer be cancelled." : "Stopping — this takes a few seconds.";
        if (!timer && job.status !== "cancelled") timer = setInterval(poll, 2000);
      }).catch(function (err) { out.textContent = err.message; out.classList.add("is-error"); cancelBtn.disabled = false; });
    });
    function share(pub) {
      var out = el("share-status"); out.className = "form-status"; out.textContent = pub ? "Creating the link…" : "Removing the link…";
      el("share-confirm").classList.add("hidden");
      postJSON(jobUrl + "/share", { public: pub }).then(function (job) { renderDelivery(job); out.textContent = pub ? "Link created — anyone with it can watch." : "Private again."; })
        .catch(function (err) { out.textContent = err.message; out.classList.add("is-error"); });
    }
    var shareBtn = el("share-btn");
    if (shareBtn) shareBtn.addEventListener("click", function () { el("share-confirm").classList.remove("hidden"); el("share-yes").focus(); });
    if (el("share-yes")) el("share-yes").addEventListener("click", function () { share(true); });
    if (el("share-no")) el("share-no").addEventListener("click", function () { el("share-confirm").classList.add("hidden"); shareBtn.focus(); });
    if (el("unshare-btn")) el("unshare-btn").addEventListener("click", function () { share(false); });

    var ytBtn = el("copy-yt-btn");
    if (ytBtn) ytBtn.addEventListener("click", function () { var f = el("yt-title"); if (!f || !f.value) return; var done = function () { ytBtn.textContent = "Copied"; setTimeout(function () { ytBtn.textContent = "Copy"; }, 1500); }; if (navigator.clipboard) navigator.clipboard.writeText(f.value).then(done, done); else { f.select(); document.execCommand("copy"); done(); } });
    var copyBtn = el("copy-link-btn");
    if (copyBtn) copyBtn.addEventListener("click", function () {
      if (!reelLink || !reelLink.value) return;
      var done = function () { copyBtn.textContent = "Copied"; setTimeout(function () { copyBtn.textContent = "Copy"; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(reelLink.value).then(done, function () { reelLink.select(); document.execCommand("copy"); done(); });
      else { reelLink.select(); document.execCommand("copy"); done(); }
    });
    if (contactLink) contactLink.addEventListener("click", function () {
      var msg = hostMsg ? hostMsg.value : "";
      var done = function () { if (hostStatus) { hostStatus.className = "form-status"; hostStatus.textContent = "Message copied — paste it into the Airbnb form (⌘V) and press Send message."; } };
      if (navigator.clipboard && msg) navigator.clipboard.writeText(msg).then(done, done); else done();
      postJSON(jobUrl + "/opened-in-browser", { message: msg }).then(renderHost).catch(function () {});
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll(".pw-toggle"), function (t) {
    t.addEventListener("click", function () { var inp = document.getElementById(t.getAttribute("data-for")); var show = inp.type === "password"; inp.type = show ? "text" : "password"; t.textContent = show ? "Hide" : "Show"; t.setAttribute("aria-label", show ? "Hide password" : "Show password"); });
  });
  var userList = document.getElementById("user-list");
  if (userList) {
    var nuStatus = document.getElementById("nu-status");
    function renderUsers(d) {
      userList.innerHTML = "";
      (d.users || []).forEach(function (u) {
        var li = document.createElement("li"); li.className = "user-row";
        li.innerHTML = "<span class=\"u-mail\"></span><span class=\"badge\"></span><span class=\"u-actions\"></span>";
        li.querySelector(".u-mail").textContent = u.user + (u.user === d.me ? " (you)" : ""); li.querySelector(".badge").textContent = u.role;
        var acts = li.querySelector(".u-actions");
        // Plan and credits: an admin can grant or correct a customer's balance by hand.
        var pl = document.createElement("div"); pl.className = "u-plan";
        var sid = "u-plan-" + Math.random().toString(36).slice(2), cid = sid + "-c";
        pl.innerHTML = '<label for="' + sid + '">Plan</label><select id="' + sid + '"></select>' +
          '<label for="' + cid + '">Credits</label><input type="number" min="0" max="100000" step="1" inputmode="numeric" id="' + cid + '">' +
          '<button type="button" class="btn btn-secondary btn-sm">Save plan</button>';
        var sel = pl.querySelector("select"), cr = pl.querySelector("input"), sv = pl.querySelector("button");
        (d.plan_keys || ["free", "starter", "commercial", "enterprise"]).forEach(function (k) {
          var o = document.createElement("option"); o.value = k; o.textContent = k.charAt(0).toUpperCase() + k.slice(1); sel.appendChild(o);
        });
        sel.value = u.plan || "free"; cr.value = u.credits == null ? 0 : u.credits;
        sv.addEventListener("click", function () {
          sv.disabled = true; nuStatus.className = "form-status";
          postJSON("/api/users/plan", { user: u.user, plan: sel.value, credits: cr.value })
            .then(function (r) { var a = r.account; nuStatus.textContent = u.user + ": " + a.plan_name + ", " + (a.remaining == null ? "unlimited" : a.remaining + " video" + (a.remaining === 1 ? "" : "s") + " left") + "."; })
            .catch(function (e) { nuStatus.textContent = e.message; nuStatus.classList.add("is-error"); })
            .then(function () { sv.disabled = false; });
        });
        var rp = document.createElement("button"); rp.type = "button"; rp.className = "btn btn-secondary btn-sm"; rp.textContent = "Reset password";
        rp.addEventListener("click", function () { var np = prompt("New password for " + u.user + " (min 8):"); if (!np) return; postJSON("/api/users/password", { user: u.user, password: np }).then(function () { nuStatus.textContent = "Password set for " + u.user; }).catch(function (e) { nuStatus.textContent = e.message; }); });
        acts.appendChild(rp);
        if (u.user !== d.me) { var rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-secondary btn-sm"; rm.textContent = "Remove";
          rm.addEventListener("click", function () { if (!confirm("Remove " + u.user + "?")) return; postJSON("/api/users/delete", { user: u.user }).then(function (dd) { getJSON("/api/users").then(renderUsers); nuStatus.textContent = dd.warning ? u.user + " removed. " + dd.warning : u.user + " removed; their Drive access was revoked."; }).catch(function (e) { nuStatus.textContent = e.message; }); });
          acts.appendChild(rm);
          // Erase is not Remove: personal data goes now; paid orders stay for the tax record period.
          var er = document.createElement("button"); er.type = "button"; er.className = "btn btn-danger btn-sm"; er.textContent = "Erase";
          er.addEventListener("click", function () { var typed = prompt("Erase " + u.user + " for good? Their reels list, outreach and account details are deleted; paid orders are kept for tax records.\nType ERASE to confirm:"); if (typed !== "ERASE") return; postJSON("/api/users/erase", { user: u.user }).then(function (dd) { getJSON("/api/users").then(renderUsers); nuStatus.textContent = u.user + " erased." + (dd.warning ? " " + dd.warning : ""); }).catch(function (e) { nuStatus.textContent = e.message; }); });
          acts.appendChild(er); }
        li.appendChild(pl);
        userList.appendChild(li);
      });
    }
    getJSON("/api/users").then(renderUsers).catch(function () {});
    document.getElementById("nu-btn").addEventListener("click", function () {
      nuStatus.className = "form-status";
      postJSON("/api/users", { user: document.getElementById("nu-email").value, password: document.getElementById("nu-pass").value, role: document.getElementById("nu-role").value })
        .then(function (dd) { getJSON("/api/users").then(renderUsers); nuStatus.textContent = "User added."; document.getElementById("nu-email").value = ""; document.getElementById("nu-pass").value = ""; })
        .catch(function (e) { nuStatus.textContent = e.message; nuStatus.classList.add("is-error"); });
    });
  }
  var pwBtn = document.getElementById("pw-btn");
  if (pwBtn) pwBtn.addEventListener("click", function () {
    var out = document.getElementById("pw-status"); out.className = "form-status";
    postJSON("/api/account/password", { current: document.getElementById("pw-current").value, new: document.getElementById("pw-new").value })
      .then(function (d) { out.textContent = "Password changed — signing you in again…"; setTimeout(function () { window.location.href = (d && d.relogin) || "/login"; }, 800); })
      .catch(function (err) { out.textContent = err.message; out.classList.add("is-error"); });
  });
  var delBtn = document.getElementById("del-btn");
  if (delBtn) delBtn.addEventListener("click", function () {
    var out = document.getElementById("del-status"), confirmed = document.getElementById("del-confirm").value.trim();
    out.className = "form-status";
    if (confirmed !== "DELETE") { out.textContent = "Type DELETE to confirm."; out.classList.add("is-error"); return; }
    delBtn.disabled = true; out.textContent = "Deleting your account…";
    postJSON("/api/account/delete", { password: document.getElementById("del-password").value, confirm: confirmed })
      .then(function (d) {
        out.textContent = "Account deleted." + (d.warning ? " " + d.warning : "");
        setTimeout(function () { window.location.href = d.redirect || "/login"; }, d.warning ? 5000 : 800);
      })
      .catch(function (err) { out.textContent = err.message; out.classList.add("is-error"); delBtn.disabled = false; });
  });
  // Google consent opens in a new tab; when the person comes back here, show the new Drive status.
  var gdForm = document.getElementById("gdrive-form");
  if (gdForm) gdForm.addEventListener("submit", function () {
    var back = function () { if (!document.hidden) window.location.reload(); };
    setTimeout(function () { document.addEventListener("visibilitychange", back); window.addEventListener("focus", back); }, 500);
  });
  var gdDisc = document.getElementById("gdrive-disconnect");
  if (gdDisc) gdDisc.addEventListener("click", function () {
    var out = document.getElementById("gdrive-status"); out.className = "form-status"; gdDisc.disabled = true;
    postJSON("/api/gdrive/disconnect").then(function (d) {
      var p = document.getElementById("gdrive-pill"); p.className = "pill pill-neg"; p.textContent = "Not connected";
      gdDisc.classList.add("hidden"); document.getElementById("gdrive-connect").textContent = "Connect Google Drive ↗";
      out.textContent = d.warning || "Disconnected, and Google confirmed the access was revoked.";
      if (d.warning) out.classList.add("is-error");
    }).catch(function (err) { out.textContent = err.message; out.classList.add("is-error"); gdDisc.disabled = false; });
  });

  // ---------- outreach console (templates/outreach.html) ----------
  var orCsrfEl = document.getElementById("or-csrf");
  if (orCsrfEl) {
    var orEscBox = document.createElement("div");
    // innerHTML leaves quotes alone; these values also go inside quoted attributes (href, src, aria-label)
    function orEsc(t) { orEscBox.textContent = t == null ? "" : String(t); return orEscBox.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
    function orPost(url, body) { var b = body || {}; b.csrf = orCsrfEl.value; return postJSON(url, b); }
    function orSay(el, msg, bad) { if (!el) return; el.className = "form-status" + (bad ? " is-error" : ""); el.textContent = msg || ""; }
    function orNum(v) { var n = parseInt(v, 10); return isNaN(n) ? v : n; }
    function orTokens(tpl, ctx) {
      var out = String(tpl == null ? "" : tpl).replace(/\{(name|city|listing_title|company)\}/g, function (m, k) {
        var v = ctx[k]; return v == null ? "" : String(v);
      });
      // an empty token must not leave a double space or a stranded comma
      return out.replace(/[ \t]{2,}/g, " ").replace(/[ \t]+([,.!?;:])/g, "$1").replace(/[ \t]+$/gm, "");
    }
    function orCopy(text, done) {
      var t = String(text == null ? "" : text);
      function fallback() {
        try {
          var ta = document.createElement("textarea");
          ta.value = t; ta.setAttribute("readonly", "");
          ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          var ok = document.execCommand("copy");
          document.body.removeChild(ta); done(!!ok);
        } catch (e) { done(false); }
      }
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(function () { done(true); }, fallback);
      else fallback();
    }
    function orFlashBtn(btn, word) {
      if (!btn) return;
      if (!btn.getAttribute("data-label")) btn.setAttribute("data-label", btn.textContent);
      btn.textContent = word;
      setTimeout(function () { btn.textContent = btn.getAttribute("data-label"); }, 1500);
    }
    function orStats(stats) {
      if (!stats) return;
      Array.prototype.forEach.call(document.querySelectorAll("[data-stat]"), function (b) {
        var k = b.getAttribute("data-stat");
        if (stats[k] != null) b.textContent = stats[k];
      });
    }
    var OR_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    function orWhen() {
      Array.prototype.forEach.call(document.querySelectorAll(".or-when"), function (el) {
        if (el.getAttribute("data-fmt")) return;
        el.setAttribute("data-fmt", "1");
        var raw = el.getAttribute("data-ts"), n = parseFloat(raw);
        if (!raw || !isFinite(n) || n < 1e8) return;
        var d = new Date(n < 1e11 ? n * 1000 : n);
        if (isNaN(d.getTime())) return;
        function p2(x) { return (x < 10 ? "0" : "") + x; }
        el.textContent = p2(d.getDate()) + " " + OR_MONTHS[d.getMonth()] + " " + d.getFullYear() + ", " + p2(d.getHours()) + ":" + p2(d.getMinutes());
        try { el.title = d.toString(); } catch (e) {}
      });
    }
    orWhen();

    // --- A. Airbnb Co-Host Network ---
    var coFind = document.getElementById("co-find"), coCity = document.getElementById("co-city");
    var coResults = document.getElementById("co-results"), coStatus = document.getElementById("co-status");
    var coCompose = document.getElementById("co-compose-card"), coPickedPill = document.getElementById("co-picked");
    var coMsg = document.getElementById("co-message"), coSendStatus = document.getElementById("co-send-status");
    var coQueueBtn = document.getElementById("co-queue");
    var coItems = [], coCityUsed = "";

    function coChecked() {
      var out = [];
      if (!coResults) return out;
      Array.prototype.forEach.call(coResults.querySelectorAll(".or-pick"), function (cb) {
        if (!cb.checked) return;
        var it = coItems[parseInt(cb.getAttribute("data-i"), 10)];
        if (it) out.push(it);
      });
      return out;
    }
    function coCount() { if (coPickedPill) coPickedPill.textContent = coChecked().length + " picked"; }
    function coPayload(list) {
      var tpl = coMsg ? coMsg.value : "";
      return list.map(function (it) {
        var city = it.city || coCityUsed;
        return {
          id: it.id != null ? it.id : null,
          name: it.name || "",
          url: it.url || "",
          airbnb_profile: it.profile_url || "",
          listing_url: it.listing_url || "",
          city: city,
          message: orTokens(tpl, { name: it.name || "", city: city, listing_title: it.listing_title || it.title || "" })
        };
      });
    }
    function coRender(data) {
      coItems = (data && data.items) || [];
      if (!coResults) return;
      coResults.innerHTML = "";
      if (!coItems.length) {
        coResults.innerHTML = '<p class="empty">No co-hosts found for that city. Try a nearby town, or the wider county.</p>';
        if (coCompose) coCompose.classList.add("hidden");
        return;
      }
      coItems.forEach(function (it, i) {
        var row = document.createElement("div");
        row.className = "or-row";
        row.innerHTML =
          '<label class="or-check"><input type="checkbox" class="or-pick" data-i="' + i + '" checked aria-label="Include ' + orEsc(it.name || "this co-host") + '"></label>' +
          (it.avatar ? '<img class="or-avatar" loading="lazy" alt="" src="' + orEsc(imgSrc(it.avatar)) + '">' : "") +
          '<div class="or-row-body"><span class="or-name">' + orEsc(it.name || "Co-host") + "</span>" +
          (it.listings != null ? '<span class="or-badge">' + orEsc(it.listings) + " listings</span>" : "") +
          (it.tagline ? '<span class="or-sub">' + orEsc(it.tagline) + "</span>" : "") + "</div>" +
          '<div class="or-row-actions">' +
          (it.url ? '<a class="btn btn-secondary btn-sm" href="' + orEsc(it.url) + '" target="_blank" rel="noopener">Open ↗</a>' : "") +
          "</div>";
        coResults.appendChild(row);
      });
      Array.prototype.forEach.call(coResults.querySelectorAll(".or-pick"), function (cb) { cb.addEventListener("change", coCount); });
      if (coCompose) coCompose.classList.remove("hidden");
      coCount();
    }
    if (coFind) coFind.addEventListener("click", function () {
      var city = ((coCity && coCity.value) || "").trim();
      if (!city) { orSay(coStatus, "Type a city first.", true); if (coCity) coCity.focus(); return; }
      coCityUsed = city;
      coFind.disabled = true;
      orSay(coStatus, "Searching the Co-Host Network for " + city + "…");
      getJSON("/api/outreach/cohosts?city=" + encodeURIComponent(city))
        .then(function (d) {
          coRender(d);
          var n = (d && d.items && d.items.length) || 0;
          var bits = [n + (n === 1 ? " co-host" : " co-hosts") + " found"];
          if (d && d.source) bits.push("source: " + d.source);
          if (d && d.note) bits.push(d.note);
          orSay(coStatus, bits.join(" · "));
        })
        .catch(function (err) { orSay(coStatus, err.message, true); })
        .then(function () { coFind.disabled = false; });
    });
    if (coCity) coCity.addEventListener("keydown", function (e) { if (e.key === "Enter" && coFind) { e.preventDefault(); coFind.click(); } });

    if (coQueueBtn) coQueueBtn.addEventListener("click", function () {
      var picked = coChecked();
      if (!picked.length) { orSay(coSendStatus, "Tick at least one co-host.", true); return; }
      if (!((coMsg && coMsg.value) || "").trim()) { orSay(coSendStatus, "Write a message first.", true); if (coMsg) coMsg.focus(); return; }
      coQueueBtn.disabled = true;
      orSay(coSendStatus, "Adding " + picked.length + " to the tracker…");
      orPost("/api/outreach/queue", { channel: "cohost", items: coPayload(picked) })
        .then(function (d) {
          orStats(d && d.stats);
          orSay(coSendStatus, "Added " + picked.length + " to the tracker · refreshing…");
          setTimeout(function () { location.reload(); }, 1200);
        })
        .catch(function (err) { orSay(coSendStatus, err.message, true); coQueueBtn.disabled = false; });
    });

    // --- B. LinkedIn prospects ---
    var liBuild = document.getElementById("li-build"), liResults = document.getElementById("li-results");
    var liStatus = document.getElementById("li-status"), liMsg = document.getElementById("li-message");
    var liCount = document.getElementById("li-count"), liCityIn = document.getElementById("li-city"), liRoleIn = document.getElementById("li-role");
    var liItems = [];

    function liCity(it) { return (it && it.city) || ((liCityIn && liCityIn.value) || "").trim(); }
    function liNote(it) {
      return orTokens(liMsg ? liMsg.value : "", { name: (it && it.name) || "", company: (it && it.company) || "", city: liCity(it) });
    }
    function liCounter() {
      if (!liCount || !liMsg) return;
      var n = liMsg.value.length;
      liCount.textContent = n + " / 300";
      liCount.classList.toggle("is-warn", n > 300);
    }
    if (liMsg) { liMsg.addEventListener("keyup", liCounter); liMsg.addEventListener("input", liCounter); liCounter(); }

    function liRender(data) {
      liItems = (data && data.items) || [];
      if (!liResults) return;
      liResults.innerHTML = "";
      if (!liItems.length) {
        liResults.innerHTML = '<p class="empty">No prospects found. Try a broader role, or a bigger city nearby.</p>';
        return;
      }
      liItems.forEach(function (it, i) {
        var sub = [it.company, it.city].filter(Boolean).join(" · ");
        var row = document.createElement("div");
        row.className = "or-row";
        row.innerHTML =
          '<div class="or-row-body"><span class="or-name">' + orEsc(it.name || "Prospect") + "</span>" +
          (sub ? '<span class="or-sub">' + orEsc(sub) + "</span>" : "") +
          (it.listings != null ? '<span class="or-badge">' + orEsc(it.listings) + " listings</span>" : "") +
          (it.note ? '<span class="or-sub">' + orEsc(it.note) + "</span>" : "") + "</div>" +
          '<div class="or-row-actions">' +
          (it.url ? '<a class="btn btn-secondary btn-sm" href="' + orEsc(it.url) + '" target="_blank" rel="noopener">' + orEsc(it.link_label || "Open ↗") + "</a>" : "") +
          (it.airbnb_profile ? '<a class="btn btn-secondary btn-sm" href="' + orEsc(it.airbnb_profile) + '" target="_blank" rel="noopener">Airbnb profile ↗</a>' : "") +
          '<button type="button" class="btn btn-secondary btn-sm li-copy" data-i="' + i + '">Copy note</button>' +
          '<button type="button" class="btn btn-primary btn-sm li-queue" data-i="' + i + '">Queue</button></div>';
        liResults.appendChild(row);
      });
      Array.prototype.forEach.call(liResults.querySelectorAll(".li-copy"), function (b) {
        b.addEventListener("click", function () {
          var it = liItems[parseInt(b.getAttribute("data-i"), 10)];
          if (!it) return;
          var text = liNote(it);
          if (!text.trim()) { orSay(liStatus, "Write a connection note first.", true); if (liMsg) liMsg.focus(); return; }
          orCopy(text, function (ok) {
            orFlashBtn(b, ok ? "Copied" : "Copy failed");
            orSay(liStatus, ok ? "Note for " + (it.name || "prospect") + " copied — paste it into LinkedIn." : "Could not copy — select the text manually.", !ok);
          });
        });
      });
      Array.prototype.forEach.call(liResults.querySelectorAll(".li-queue"), function (b) {
        b.addEventListener("click", function () {
          var it = liItems[parseInt(b.getAttribute("data-i"), 10)];
          if (!it) return;
          b.disabled = true;
          orPost("/api/outreach/queue", {
            channel: "linkedin",
            items: [{ id: it.id != null ? it.id : null, name: it.name || "", url: it.url || "", city: liCity(it), message: liNote(it), airbnb_profile: it.airbnb_profile || "", listing_url: it.listing_url || "" }]
          })
            .then(function (d) { orStats(d && d.stats); b.textContent = "Queued"; orSay(liStatus, (it.name || "Prospect") + " added to the tracker — reload to see the row."); })
            .catch(function (err) { orSay(liStatus, err.message, true); b.disabled = false; });
        });
      });
    }
    if (liBuild) liBuild.addEventListener("click", function () {
      var city = ((liCityIn && liCityIn.value) || "").trim();
      var role = ((liRoleIn && liRoleIn.value) || "").trim();
      if (!city) { orSay(liStatus, "Type a city first.", true); if (liCityIn) liCityIn.focus(); return; }
      liBuild.disabled = true;
      orSay(liStatus, "Building the prospect list…");
      getJSON("/api/outreach/linkedin?city=" + encodeURIComponent(city) + "&role=" + encodeURIComponent(role))
        .then(function (d) {
          liRender(d);
          var n = (d && d.items && d.items.length) || 0;
          orSay(liStatus, n + (n === 1 ? " prospect" : " prospects") + (d && d.source ? " · source: " + d.source : ""));
        })
        .catch(function (err) { orSay(liStatus, err.message, true); })
        .then(function () { liBuild.disabled = false; });
    });

    // --- C. Tracker ---
    // rows carry scraped URLs: never let a non-http(s) scheme stay clickable
    Array.prototype.forEach.call(document.querySelectorAll("#tracker-card a[href]"), function (a) {
      if (!/^https?:\/\//i.test(a.getAttribute("href") || "")) { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); a.classList.add("is-disabled"); a.title = "Link removed: not an http(s) address"; }
    });
    var trChan = document.getElementById("tr-channel"), trStat = document.getElementById("tr-status"), trSearch = document.getElementById("tr-search");
    var trTable = document.getElementById("tr-table"), trNone = document.getElementById("tr-none");
    function trFilter() {
      if (!trTable) return;
      var c = trChan ? trChan.value : "", s = trStat ? trStat.value : "", q = (trSearch ? trSearch.value : "").trim().toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(trTable.querySelectorAll("tbody tr"), function (tr) {
        var ok = (!c || tr.getAttribute("data-channel") === c) &&
          (!s || tr.getAttribute("data-status") === s) &&
          (!q || (tr.getAttribute("data-search") || "").indexOf(q) !== -1);
        tr.classList.toggle("hidden", !ok);
        if (ok) shown += 1;
      });
      if (trNone) trNone.classList.toggle("hidden", shown !== 0);
    }
    if (trChan) trChan.addEventListener("change", trFilter);
    if (trStat) trStat.addEventListener("change", trFilter);
    if (trSearch) trSearch.addEventListener("input", trFilter);

    Array.prototype.forEach.call(document.querySelectorAll(".tr-status-sel"), function (sel) {
      sel.setAttribute("data-prev", sel.value);
      sel.addEventListener("change", function () {
        var val = sel.value, prev = sel.getAttribute("data-prev") || "";
        var row = sel.closest ? sel.closest("tr") : null;
        sel.disabled = true;
        orPost("/api/outreach/status", { id: orNum(sel.getAttribute("data-id")), status: val })
          .then(function (d) {
            orStats(d && d.stats);
            sel.setAttribute("data-prev", val);
            if (row) row.setAttribute("data-status", val);
            trFilter();
          })
          .catch(function (err) { sel.value = prev; window.alert(err.message); })
          .then(function () { sel.disabled = false; });
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll(".tr-copy"), function (b) {
      b.addEventListener("click", function () {
        var msg = b.getAttribute("data-msg") || "";
        if (!msg.trim()) { orFlashBtn(b, "No message"); return; }
        orCopy(msg, function (ok) { orFlashBtn(b, ok ? "Copied" : "Copy failed"); });
      });
    });

    // Do not contact: an objection is honoured for every ReelSieve user, and the row goes.
    Array.prototype.forEach.call(document.querySelectorAll(".tr-suppress"), function (b) {
      b.addEventListener("click", function () {
        if (!window.confirm("They asked not to be contacted? This removes them from your tracker and from every ReelSieve user's results.")) return;
        b.disabled = true;
        orPost("/api/outreach/suppress", { id: orNum(b.getAttribute("data-id")) })
          .then(function (d) { orStats(d && d.stats); var row = b.closest ? b.closest("tr") : null; if (row) row.parentNode.removeChild(row); trFilter(); })
          .catch(function (err) { window.alert(err.message); b.disabled = false; });
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll(".tr-note"), function (b) {
      b.addEventListener("click", function () {
        var note = window.prompt("Note for this prospect:", b.getAttribute("data-note") || "");
        if (note === null) return;
        b.disabled = true;
        orPost("/api/outreach/note", { id: orNum(b.getAttribute("data-id")), note: note })
          .then(function () {
            b.setAttribute("data-note", note);
            var row = b.closest ? b.closest("tr") : null;
            var box = row ? row.querySelector(".or-note") : null;
            if (box) { box.textContent = note; box.classList.toggle("hidden", !note); }
            orFlashBtn(b, "Saved");
          })
          .catch(function (err) { window.alert(err.message); })
          .then(function () { b.disabled = false; });
      });
    });
  }

  // ---------- billing: request an invoice ----------
  var billBtn = document.getElementById("bill-request");
  if (billBtn) {
    billBtn.addEventListener("click", function () {
      var out = document.getElementById("bill-status"); out.className = "form-status"; billBtn.disabled = true; out.textContent = "Creating your order…";
      var note = (document.getElementById("bill-note") || {}).value || "";
      postJSON("/api/billing/request", { plan: billBtn.getAttribute("data-plan"), provider: "invoice", note: note, csrf: (document.getElementById("bill-csrf") || {}).value })
        .then(function (d) { window.location.href = "/upgrade?plan=" + encodeURIComponent(billBtn.getAttribute("data-plan")) + "&ref=" + encodeURIComponent(d.order.ref); })
        .catch(function (e) { out.textContent = e.message || "Could not create the order."; out.classList.add("is-error"); billBtn.disabled = false; });
    });
  }

  // ---------- admin: privacy requests ----------
  Array.prototype.forEach.call(document.querySelectorAll(".req-done"), function (b) {
    b.addEventListener("click", function () {
      var out = document.getElementById("req-status"), ref = b.getAttribute("data-ref");
      if (!confirm("Mark " + ref + " handled? Do this once you have replied.")) return;
      b.disabled = true; out.className = "form-status";
      postJSON("/api/privacy-requests/handled", { ref: ref })
        .then(function () { var li = b.closest("li"); if (li) li.parentNode.removeChild(li); out.textContent = ref + " marked handled."; })
        .catch(function (e) { out.textContent = e.message; out.classList.add("is-error"); b.disabled = false; });
    });
  });

  // ---------- admin: orders ----------
  var ordList = document.getElementById("ord-list");
  if (ordList) {
    var ordStatus = document.getElementById("ord-status"), ordPending = document.getElementById("ord-pending");
    function renderOrders(d) {
      ordList.innerHTML = ""; var rows = (d && d.orders) || [];
      ordPending.textContent = (d && d.pending ? d.pending : 0) + " pending";
      if (!rows.length) { ordList.innerHTML = '<li class="user-row"><span>No orders yet.</span></li>'; return; }
      rows.forEach(function (o) {
        var li = document.createElement("li"); li.className = "user-row";
        var when = o.ts ? new Date(o.ts * 1000).toLocaleDateString() : "";
        li.innerHTML = '<span class="u-mail"></span><span class="badge"></span><span class="u-actions"></span>';
        li.querySelector(".u-mail").textContent = o.ref + " · " + o.user + " · " + o.plan + " · $" + Math.round(o.amount_usd) + " · " + when + (o.pay_link ? " · link sent" : "");
        li.querySelector(".badge").textContent = o.status;
        var acts = li.querySelector(".u-actions");
        if (o.status === "pending" || o.status === "reported") {
          var pay = document.createElement("button"); pay.type = "button"; pay.className = "btn btn-primary btn-sm"; pay.textContent = "Mark paid";
          pay.addEventListener("click", function () {
            if (!confirm("Mark " + o.ref + " paid and grant " + o.plan + " credits to " + o.user + "?")) return;
            postJSON("/api/billing/settle", { ref: o.ref }).then(function (r) { ordStatus.textContent = o.ref + " settled — " + r.account.plan_name + ", " + r.account.remaining + " credits."; load(); })
              .catch(function (e) { ordStatus.textContent = e.message; });
          });
          var can = document.createElement("button"); can.type = "button"; can.className = "btn btn-secondary btn-sm"; can.textContent = "Cancel";
          can.addEventListener("click", function () { if (!confirm("Cancel " + o.ref + "?")) return; postJSON("/api/billing/cancel", { ref: o.ref }).then(load).catch(function (e) { ordStatus.textContent = e.message; }); });
          // Providers like Skydo mint one single-use link per payment, so the link IS the tenant mapping.
          var lnk = document.createElement("button"); lnk.type = "button"; lnk.className = "btn btn-secondary btn-sm";
          lnk.textContent = o.pay_link ? "Change link" : "Add pay link";
          lnk.addEventListener("click", function () {
            var url = prompt("Paste the payment link issued for " + o.ref + " (" + o.user + ", $" + Math.round(o.amount_usd) + ").\nLeave blank to remove it.", o.pay_link || "");
            if (url === null) return;
            postJSON("/api/billing/link", { ref: o.ref, url: url.trim() })
              .then(function () { ordStatus.textContent = url.trim() ? (o.ref + " — link attached; the customer sees a Pay button.") : (o.ref + " — link removed."); load(); })
              .catch(function (e) { ordStatus.textContent = e.message; });
          });
          acts.appendChild(pay); acts.appendChild(can); acts.appendChild(lnk);
        }
        ordList.appendChild(li);
      });
    }
    function load() { getJSON("/api/billing/orders?all=1").then(renderOrders).catch(function () {}); }
    load();
  }

  // ---------- billing: checkout (Stripe when configured, else a reusable payment link) ----------
  Array.prototype.forEach.call(document.querySelectorAll(".js-checkout"), function (payBtn) {
    payBtn.addEventListener("click", function () {
      var out = document.getElementById(payBtn.getAttribute("data-status")); out.className = "form-status"; payBtn.disabled = true; out.textContent = "Creating your order…";
      postJSON("/api/billing/start", { plan: payBtn.getAttribute("data-plan") })
        .then(function (d) { out.textContent = "Order " + d.order.ref + ". Taking you to payment…"; window.location.href = d.pay_url; })
        .catch(function (e) { out.textContent = e.message || "Could not start the payment."; out.classList.add("is-error"); payBtn.disabled = false; });
    });
  });
})();
