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
    function renderHost(job) {
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
