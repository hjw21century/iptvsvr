/* IPTV-Hub 前端：全量拉取一次频道数据，之后本地过滤/排序，交互零延迟。 */
(function () {
  "use strict";

  var state = {
    channels: [],
    filtered: [],
    group: "",
    sort: { key: "score", desc: true },
    pageSize: 300,
    static: false,
    shown: 0
  };

  var el = function (id) { return document.getElementById(id); };

  function toast(message) {
    var node = el("toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.classList.remove("show"); }, 1800);
  }

  function height(channel) {
    var resolution = channel.resolution || "";
    var parts = resolution.toLowerCase().split("x");
    return parts.length === 2 ? parseInt(parts[1], 10) || 0 : 0;
  }

  function qualityBadge(channel) {
    var h = height(channel);
    if (!h) return '<span class="badge">未知</span>';
    var cls = h >= 720 ? "hd" : "sd";
    var label = h >= 2000 ? "4K" : h + "p";
    return '<span class="badge ' + cls + '">' + label + "</span>";
  }

  function bar(value) {
    var pct = Math.max(0, Math.min(1, value)) * 100;
    var cls = value >= 0.7 ? "good" : value >= 0.4 ? "mid" : "bad";
    return '<div class="bar ' + cls + '"><i style="width:' + pct.toFixed(0) + '%"></i></div>';
  }

  function humanKbps(kbps) {
    if (!kbps) return "—";
    return kbps >= 1000 ? (kbps / 1000).toFixed(1) + " Mbps" : Math.round(kbps) + " kbps";
  }

  // ------------------------------------------------------------ 订阅链接
  function params(extra) {
    var search = new URLSearchParams();
    if (state.group) search.set("group", state.group);
    var q = el("q").value.trim();
    if (q) search.set("q", q);
    if (el("ipv").value !== "0") search.set("ipv", el("ipv").value);
    if (el("quality").value !== "0") search.set("min_height", el("quality").value);
    if (el("minScore").value !== "0") search.set("min_score", el("minScore").value);
    if (el("backups").checked) search.set("backups", "1");
    Object.keys(extra || {}).forEach(function (key) { search.set(key, extra[key]); });
    var text = search.toString();
    return text ? "?" + text : "";
  }

  function subscriptionUrl() {
    if (state.static) {
      return new URL("playlist.m3u", location.href).href;  // 静态导出模式没有动态过滤
    }
    return location.origin + "/playlist.m3u" + params();
  }

  function updateSubUrl() {
    el("subUrl").textContent = subscriptionUrl();
  }

  // ---------------------------------------------------------------- 渲染
  function applyFilters() {
    var q = el("q").value.trim().toLowerCase();
    var minHeight = parseInt(el("quality").value, 10) || 0;
    var ipv = parseInt(el("ipv").value, 10) || 0;
    var minScore = parseFloat(el("minScore").value) || 0;

    state.filtered = state.channels.filter(function (channel) {
      if (state.group && channel.group !== state.group) return false;
      if (q && channel.name.toLowerCase().indexOf(q) < 0) return false;
      if (minHeight && height(channel) < minHeight) return false;
      if (minScore && (channel.score || 0) < minScore) return false;
      if (ipv) {
        return (channel.streams || []).some(function (s) { return s.ip_version === ipv; });
      }
      return true;
    });

    var key = state.sort.key;
    var desc = state.sort.desc;
    state.filtered.sort(function (a, b) {
      var va = key === "height" ? height(a) : a[key];
      var vb = key === "height" ? height(b) : b[key];
      if (typeof va === "string" || typeof vb === "string") {
        va = String(va || ""); vb = String(vb || "");
        return desc ? vb.localeCompare(va, "zh") : va.localeCompare(vb, "zh");
      }
      va = va || 0; vb = vb || 0;
      return desc ? vb - va : va - vb;
    });

    state.shown = 0;
    el("rows").innerHTML = "";
    renderMore();
    updateSubUrl();
  }

  function renderMore() {
    var slice = state.filtered.slice(state.shown, state.shown + state.pageSize);
    var html = slice.map(function (channel, index) {
      var position = state.shown + index + 1;
      var backups = (channel.streams || []).length - 1;
      return "<tr>" +
        '<td class="num">' + position + "</td>" +
        '<td class="hide-sm"><img class="logo" loading="lazy" src="' + (channel.logo || "") +
          '" onerror="this.style.visibility=\'hidden\'"></td>' +
        '<td class="name">' + escapeHtml(channel.name) +
          (backups > 0 ? '<span class="backup">+' + backups + " 备用</span>" : "") + "</td>" +
        '<td class="hide-sm">' + escapeHtml(channel.group || "") + "</td>" +
        '<td class="hide-sm">' + qualityBadge(channel) + "</td>" +
        '<td class="num hide-sm">' + humanKbps(channel.kbps) + "</td>" +
        '<td class="num hide-sm">' + (channel.ttfb_ms ? Math.round(channel.ttfb_ms) + " ms" : "—") + "</td>" +
        '<td class="num hide-sm">' + Math.round((channel.uptime || 0) * 100) + "%</td>" +
        "<td>" + bar(channel.score || 0) + "</td>" +
        '<td><div class="row-actions">' +
          '<button data-copy="' + escapeHtml(channel.url) + '">复制</button>' +
          '<button data-play="' + escapeHtml(channel.key) + '">播放</button>' +
        "</div></td>" +
      "</tr>";
    }).join("");

    el("rows").insertAdjacentHTML("beforeend", html);
    state.shown += slice.length;

    el("table").style.display = state.filtered.length ? "" : "none";
    el("empty").style.display = state.filtered.length ? "none" : "";
    var more = el("more");
    if (state.shown < state.filtered.length) {
      more.style.display = "";
      more.innerHTML = '<button id="moreBtn">继续加载（已显示 ' + state.shown + " / " +
        state.filtered.length + "）</button>";
      el("moreBtn").onclick = renderMore;
    } else {
      more.style.display = state.filtered.length > 0 ? "" : "none";
      more.textContent = "共 " + state.filtered.length + " 个频道";
    }
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderGroups() {
    var counter = {};
    var order = [];
    state.channels.forEach(function (channel) {
      var group = channel.group || "其他频道";
      if (!(group in counter)) { counter[group] = 0; order.push(group); }
      counter[group] += 1;
    });
    var html = ['<span class="chip' + (state.group ? "" : " active") + '" data-group="">全部' +
      '<span class="n">' + state.channels.length + "</span></span>"];
    order.forEach(function (group) {
      html.push('<span class="chip' + (state.group === group ? " active" : "") +
        '" data-group="' + escapeHtml(group) + '">' + escapeHtml(group) +
        '<span class="n">' + counter[group] + "</span></span>");
    });
    el("groupChips").innerHTML = html.join("");
  }

  // ---------------------------------------------------------------- 播放
  var hlsInstance = null;

  function loadHls(callback) {
    if (window.Hls) return callback();
    var script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js";
    script.onload = callback;
    script.onerror = function () { callback(); };
    document.head.appendChild(script);
  }

  function play(channel, url) {
    var video = el("player");
    el("modalTitle").textContent = channel.name;
    el("modal").classList.add("open");

    el("streamList").innerHTML = (channel.streams || []).map(function (stream, index) {
      return "<div><span>" + (index === 0 ? "主源" : "备用" + index) + "</span>" +
        "<code>" + escapeHtml(stream.url) + "</code>" +
        "<span>" + humanKbps(stream.kbps) + "</span>" +
        '<button data-switch="' + escapeHtml(stream.url) + '">播放</button></div>';
    }).join("");

    var target = url || channel.url;
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }

    loadHls(function () {
      if (window.Hls && window.Hls.isSupported() && /\.m3u8(\?|$)/i.test(target)) {
        hlsInstance = new window.Hls({ maxBufferLength: 10 });
        hlsInstance.loadSource(target);
        hlsInstance.attachMedia(video);
        hlsInstance.on(window.Hls.Events.ERROR, function (_e, data) {
          if (data.fatal) toast("播放失败：" + data.details);
        });
      } else {
        video.src = target;
      }
      video.play().catch(function () { /* 需要用户手势，忽略 */ });
    });
  }

  function closeModal() {
    el("modal").classList.remove("open");
    var video = el("player");
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
  }

  // ---------------------------------------------------------------- 数据
  /* 既支持后端 API，也支持"纯静态导出"模式（无 API 时回退读取同目录 channels.json） */
  function fetchJson(primary, fallback) {
    return fetch(primary).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).catch(function () {
      return fetch(fallback).then(function (r) { return r.json(); });
    });
  }

  function loadStats() {
    fetchJson("/api/stats", "channels.json").then(function (payload) {
      var stats = payload.stats ? Object.assign({}, payload.stats, {
        generated_at: payload.generated_at,
        channel_count: payload.channel_count
      }) : payload;
      el("tChannels").textContent = stats.channel_count || stats.channels_total || 0;
      el("tStreams").textContent = (stats.streams_alive || 0) + " / " + (stats.streams_total || 0);
      el("tLatency").innerHTML = Math.round(stats.avg_ttfb_ms || 0) + "<small>ms</small>";
      el("tKbps").innerHTML = Math.round(stats.avg_kbps || 0) + "<small>kbps</small>";
      el("tRun").textContent = stats.updating ? "更新中…" : (stats.generated_at || "—");
      el("generatedAt").textContent = "数据版本 " + (stats.generated_at || "—");
    }).catch(function () { /* 忽略 */ });
  }

  function loadChannels() {
    el("loading").style.display = "";
    fetchJson("/api/channels", "channels.json").then(function (payload) {
      state.channels = payload.channels || [];
      state.static = !payload.total && !payload.count;
      el("loading").style.display = "none";
      renderGroups();
      applyFilters();
    }).catch(function (err) {
      el("loading").textContent = "加载失败：" + err;
    });
  }

  // ---------------------------------------------------------------- 事件
  function bind() {
    ["q", "quality", "ipv", "minScore"].forEach(function (id) {
      el(id).addEventListener("input", applyFilters);
    });
    el("backups").addEventListener("change", updateSubUrl);

    el("groupChips").addEventListener("click", function (event) {
      var chip = event.target.closest(".chip");
      if (!chip) return;
      state.group = chip.getAttribute("data-group") || "";
      renderGroups();
      applyFilters();
    });

    document.querySelectorAll("thead th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort");
        if (state.sort.key === key) {
          state.sort.desc = !state.sort.desc;
        } else {
          state.sort = { key: key, desc: key !== "name" && key !== "group" };
        }
        document.querySelectorAll("thead th").forEach(function (node) {
          node.classList.remove("sorted");
        });
        th.classList.add("sorted");
        applyFilters();
      });
    });

    el("rows").addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.copy) {
        copy(button.dataset.copy);
      } else if (button.dataset.play) {
        var channel = state.channels.find(function (c) { return c.key === button.dataset.play; });
        if (channel) play(channel);
      }
    });

    el("streamList").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-switch]");
      if (!button) return;
      var title = el("modalTitle").textContent;
      var channel = state.channels.find(function (c) { return c.name === title; });
      if (channel) play(channel, button.dataset.switch);
    });

    el("copySub").onclick = function () { copy(subscriptionUrl()); };
    el("openSub").onclick = function () {
      location.href = state.static ? "playlist.m3u" : "/playlist.m3u" + params();
    };
    el("refreshBtn").onclick = function () { loadStats(); loadChannels(); };
    el("modalClose").onclick = closeModal;
    el("modal").addEventListener("click", function (event) {
      if (event.target === el("modal")) closeModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeModal();
    });
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { toast("已复制"); });
      return;
    }
    var input = document.createElement("textarea");
    input.value = text;
    document.body.appendChild(input);
    input.select();
    try { document.execCommand("copy"); toast("已复制"); } catch (e) { toast("复制失败"); }
    document.body.removeChild(input);
  }

  bind();
  loadStats();
  loadChannels();
  setInterval(loadStats, 60000);
})();
