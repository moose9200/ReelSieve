/* Listing Reel — small vanilla JS: theme toggle, job submit, job polling, host message, settings actions. */
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

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { detail: t }; }
        if (!r.ok) {
          var msg = (data && (data.detail || data.error || data.message)) || ("Request failed (" + r.status + ")");
          if (typeof msg !== "string") msg = JSON.stringify(msg);
          throw new Error(msg);
        }
        return data;
      });
    });
  }
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
        el.innerHTML = (it.photo ? '<img loading="lazy" src="' + esc(it.photo) + '?im_w=720" alt="">' : "") +
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
      postJSON("/api/jobs", { url: url, send_to_host: sendToHost, message: message, ai_motion: aiMotion, style: style })
        .then(function (data) {
          var id = data && (data.id || data.job_id || (data.job && data.job.id));
          if (!id) throw new Error("Server did not return a job id.");
          window.location.href = "/jobs/" + encodeURIComponent(id);
        })
        .catch(function (err) {
          btn.disabled = false;
          status.textContent = err.message || "Something went wrong.";
          status.classList.add("is-error");
        });
    });
  }

  // ---------- job: poll + host message ----------
  var jobEl = document.getElementById("job");
  if (jobEl) {
    var jobId = jobEl.getAttribute("data-job-id");
    var el = function (id) { return document.getElementById(id); };
    var pill = el("status-pill"), step = el("step"), pct = el("pct"), fill = el("progress-fill"),
        bar = el("progress"), errEl = el("error"), log = el("log"), videoCard = el("video-card"), hostCard = el("host-card"),
        video = el("video"), dl = el("download-btn"), hostPill = el("host-pill"), reelLink = el("reel-link"),
        hostMsg = el("host-message"), hostStatus = el("host-status"), contactLink = el("contact-link"),
        title = el("listing-title"), loc = el("listing-location"), meta = el("listing-meta");
    var timer = null, lastLogLen = -1, msgTouched = false;
    if (hostMsg) hostMsg.addEventListener("input", function () { msgTouched = true; });

    function setPill(node, status) { node.className = "pill pill-" + status; node.textContent = status; }
    function hostText(job) {
      var s = job.host_status || "";
      if (s === "sent") return "Sent to host";
      if (s === "draft") return "Opened in Airbnb — paste and press Send";
      if (s === "skipped") return "Not sent" + (job.host_error ? " — " + job.host_error : "");
      if (s === "failed") return "Failed" + (job.host_error ? " — " + job.host_error : "");
      return s || "—";
    }
    function renderDrive(job) {
      var dp = el("drive-pill"), dl2 = el("drive-link"), ub = el("drive-upload-btn"); if (!dp) return;
      var st = job.drive_status || "";
      dp.className = "pill pill-email pill-" + (st === "uploaded" ? "done" : st === "failed" ? "failed" : "skipped");
      dp.textContent = st === "uploaded" ? "Uploaded" + (job.drive_name ? " · " + job.drive_name : "") : st === "failed" ? "Failed — " + (job.drive_error || "") : st === "skipped" ? "Not uploaded — " + (job.drive_error || "") : "—";
      if (dl2) { dl2.classList.toggle("hidden", !job.drive_link); if (job.drive_link) dl2.href = job.drive_link; }
      if (ub) ub.classList.toggle("hidden", st === "uploaded");
    }
    var driveBtn = el("drive-upload-btn");
    if (driveBtn) driveBtn.addEventListener("click", function () {
      driveBtn.disabled = true; driveBtn.textContent = "Uploading…";
      postJSON("/api/jobs/" + encodeURIComponent(jobId) + "/upload-drive").then(function (d) { renderDrive(d.job || d); renderHost(d.job || d); })
        .catch(function (err) { alert(err.message || "Upload failed"); }).then(function () { driveBtn.disabled = false; driveBtn.textContent = "Upload to Drive"; });
    });
    function renderHost(job) {
      renderDrive(job);
      if (hostPill) {
        hostPill.setAttribute("data-status", job.host_status || "");
        hostPill.className = "pill pill-email pill-" + (job.host_status === "sent" || job.host_status === "draft" ? "done" : (job.host_status === "failed" ? "failed" : "skipped"));
        hostPill.textContent = hostText(job);
      }
      if (reelLink && job.reel_link && reelLink.value !== job.reel_link) reelLink.value = job.reel_link;
      if (hostMsg && !msgTouched && (job.message_final || job.message)) hostMsg.value = job.message_final || job.message;
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
      if (st === "done" && job.video_url) {
        if (video.getAttribute("src") !== job.video_url) { video.src = job.video_url; video.load(); }
        dl.href = job.video_url;
        videoCard.classList.remove("hidden");
        if (hostCard) hostCard.classList.remove("hidden");
        renderHost(job);
      }
      if (st === "done" || st === "failed") stop();
    }
    function poll() {
      getJSON("/api/jobs/" + encodeURIComponent(jobId)).then(function (data) { render(data.job || data); }).catch(function () {});
    }
    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    var initial = jobEl.getAttribute("data-status");
    if (initial !== "done" && initial !== "failed") {
      poll(); timer = setInterval(poll, 2000);
      document.addEventListener("visibilitychange", function () { if (!document.hidden && timer) poll(); });
    } else { poll(); }

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
      postJSON("/api/jobs/" + encodeURIComponent(jobId) + "/opened-in-browser", { message: msg }).then(function (d) { renderHost(d.job || d); }).catch(function () {});
    });
    var sendBtn = el("send-host-btn");
    if (sendBtn) sendBtn.addEventListener("click", function () {
      sendBtn.disabled = true; hostStatus.className = "form-status"; hostStatus.textContent = "Opening Airbnb…";
      postJSON("/api/jobs/" + encodeURIComponent(jobId) + "/send-to-host", { message: hostMsg ? hostMsg.value : "" })
        .then(function (data) { renderHost(data.job || data); hostStatus.textContent = (data && (data.host_error || data.message)) || "Opened. Review the message in the Airbnb window and press Send message."; })
        .catch(function (err) { hostStatus.textContent = err.message || "Could not open Airbnb."; hostStatus.classList.add("is-error"); })
        .then(function () { sendBtn.disabled = false; });
    });
  }

  // ---------- settings: Airbnb connect + tunnel ----------
  var connectBtn = document.getElementById("connect-airbnb-btn");
  if (connectBtn) {
    var aPill = document.getElementById("airbnb-pill"), aStatus = document.getElementById("airbnb-status"), discBtn = document.getElementById("disconnect-airbnb-btn");
    function setAirbnb(d) {
      var ok = !!(d && d.connected);
      aPill.className = "pill " + (ok ? "pill-done" : "pill-neg"); aPill.textContent = ok ? "Connected" : "Not connected";
      return ok;
    }
    connectBtn.addEventListener("click", function () {
      connectBtn.disabled = true; aStatus.className = "form-status"; aStatus.textContent = "A Chrome window is opening — log in to Airbnb there, then close it.";
      postJSON("/api/airbnb/connect").then(function () {
        var tries = 0;
        var t = setInterval(function () {
          tries += 1;
          getJSON("/api/airbnb/status").then(function (d) {
            if (setAirbnb(d)) { clearInterval(t); aStatus.textContent = "Connected."; connectBtn.disabled = false; }
            else if (tries > 60) { clearInterval(t); aStatus.textContent = "Still not connected — try again."; connectBtn.disabled = false; }
          }).catch(function () {});
        }, 3000);
      }).catch(function (err) { aStatus.textContent = err.message; aStatus.classList.add("is-error"); connectBtn.disabled = false; });
    });
    if (discBtn) discBtn.addEventListener("click", function () {
      postJSON("/api/airbnb/disconnect").then(function (d) { setAirbnb(d); aStatus.textContent = "Disconnected."; }).catch(function (err) { aStatus.textContent = err.message; });
    });
  }
  var gdDisc = document.getElementById("gdrive-disconnect");
  if (gdDisc) gdDisc.addEventListener("click", function () {
    postJSON("/api/gdrive/disconnect").then(function (d) { var p = document.getElementById("gdrive-pill"); p.className = "pill pill-neg"; p.textContent = "Not connected"; document.getElementById("gdrive-status").textContent = "Disconnected."; }).catch(function (err) { document.getElementById("gdrive-status").textContent = err.message; });
  });
  var tStart = document.getElementById("tunnel-start-btn");
  if (tStart) {
    var tPill = document.getElementById("tunnel-pill"), tUrl = document.getElementById("tunnel-url"), tStatus = document.getElementById("tunnel-status"), tStop = document.getElementById("tunnel-stop-btn");
    function setTunnel(d) {
      var on = !!(d && d.running);
      tPill.className = "pill " + (on ? "pill-done" : "pill-neg"); tPill.textContent = on ? "Tunnel live" : "No tunnel";
      tUrl.textContent = "";
      if (d && d.url) { tUrl.appendChild(document.createTextNode("Tunnel URL: ")); var a = document.createElement("a"); a.href = d.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = d.url; tUrl.appendChild(a); }
      if (d && d.error) { tStatus.textContent = d.error; tStatus.classList.add("is-error"); }
    }
    tStart.addEventListener("click", function () {
      tStart.disabled = true; tStatus.className = "form-status"; tStatus.textContent = "Starting tunnel…";
      postJSON("/api/tunnel/start").then(function (d) { setTunnel(d); tStatus.textContent = d && d.url ? "Live." : (d && d.error) || "No URL yet — try again."; })
        .catch(function (err) { tStatus.textContent = err.message; tStatus.classList.add("is-error"); })
        .then(function () { tStart.disabled = false; });
    });
    if (tStop) tStop.addEventListener("click", function () {
      postJSON("/api/tunnel/stop").then(function (d) { setTunnel(d); tStatus.textContent = "Stopped."; }).catch(function (err) { tStatus.textContent = err.message; });
    });
  }
})();
