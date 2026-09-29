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
  var currentChannel = null;
  var sessionTimer = null;
  var PREVIEW_MINUTES = 10;   // 网页预览时长上限，到点自动暂停，避免后台标签页一直耗流量

  /* 能直连就直连：源本身是 https 且带 CORS 头时，浏览器可以自己去拉，
     一点本站带宽都不占。其余的（http 源 / 不给 CORS）才经本站中转——
     浏览器有混合内容与跨域两条限制，VLC 等播放器没有。 */
  function streamOf(channel, url) {
    var list = channel.streams || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].url === url) return list[i];
    }
    return list[0] || { url: url, direct: false };
  }

  function playable(stream, forceProxy) {
    if (stream.direct && !forceProxy) return stream.url;
    return "/proxy?u=" + encodeURIComponent(stream.url);
  }

  function loadHls(callback) {
    if (window.Hls) return callback();
    var script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js";
    script.onload = callback;
    script.onerror = function () { callback(); };
    document.head.appendChild(script);
  }

  /* ------------------------------------------------------------ 反馈 */
  var FEEDBACK_KINDS = [
    ["ok", "能正常看"], ["lag", "卡顿/缓冲"], ["black", "黑屏/花屏"],
    ["nosound", "没有声音"], ["dead", "打不开"], ["other", "其它"]
  ];
  var feedbackState = { summary: {}, items: [], openUrl: "", you: "" };

  function feedbackBadges(url) {
    var counts = feedbackState.summary[url] || {};
    var good = counts.ok || 0;
    var bad = (counts.lag || 0) + (counts.black || 0) + (counts.nosound || 0) + (counts.dead || 0);
    var parts = [];
    if (good) parts.push('<span class="fb-good">👍 ' + good + "</span>");
    if (bad) parts.push('<span class="fb-bad">⚠ ' + bad + "</span>");
    return parts.join(" ");
  }

  function renderStreams(channel) {
    var head = '<div class="stream-tip"><b>网页播放仅供试看</b>，10 分钟自动暂停；' +
      '标「直连」的源由浏览器直接拉取，标「中转」的源受每日配额限制。<br>' +
      '想稳定观看，请用 <b>VLC</b> 等直播专用播放器：' +
      '<a href="/channel.m3u?key=' + encodeURIComponent(channel.key) +
      '" download>下载本频道 m3u</a> · ' +
      '<a href="#" data-guide="1">查看使用方法</a></div>';

    el("streamList").innerHTML = head + (channel.streams || []).map(function (stream, index) {
      var label = index === 0 ? "主源" : "备用" + index;
      var mine = feedbackState.items.filter(function (item) { return item.url === stream.url; });
      var open = feedbackState.openUrl === stream.url;
      return '<div class="stream-row" data-url="' + escapeHtml(stream.url) + '">' +
        '<div class="stream-main">' +
          '<span class="stream-label">' + label + "</span>" +
          "<code>" + escapeHtml(stream.url) + "</code>" +
          '<span class="stream-rate">' + humanKbps(stream.kbps) + "</span>" +
          '<button data-switch="' + escapeHtml(stream.url) + '">播放</button>' +
        "</div>" +
        '<div class="stream-sub">' +
          '<span class="badge ' + (stream.direct ? "hd" : "") + '">' +
            (stream.direct ? "直连" : "中转") + "</span>" +
          feedbackBadges(stream.url) +
          '<button class="link" data-fb="' + escapeHtml(stream.url) + '">' +
            (open ? "收起反馈" : "反馈这条源") +
            (mine.length ? "（" + mine.length + "）" : "") + "</button>" +
        "</div>" +
        '<div class="fb-panel' + (open ? "" : " hidden") +
          '" data-panel="' + escapeHtml(stream.url) + '">' +
          '<div class="fb-hint">看到什么情况，<b>点一下就提交</b>，不用填任何信息。' +
            "系统会自动记录你的来源 IP（" + escapeHtml(feedbackState.you || "自动识别") +
            "），仅用于识别重复提交，不可修改。</div>" +
          '<div class="fb-kinds">' + FEEDBACK_KINDS.map(function (kind) {
            return '<button class="chip" data-kind="' + kind[0] +
              '" data-url="' + escapeHtml(stream.url) + '">' + kind[1] + "</button>";
          }).join("") + "</div>" +
          '<div class="fb-more">' +
            '<textarea class="fb-msg" maxlength="300" ' +
              'placeholder="想多说两句？（选填，例如：晚上八点后卡顿严重）"></textarea>' +
            '<div class="fb-actions"><span class="sub">填了说明就点这里提交，' +
              "默认归为「其它」</span>" +
              '<button class="primary" data-send="' + escapeHtml(stream.url) + '">提交说明</button>' +
            "</div>" +
          "</div>" +
          '<div class="fb-list">' + (mine.length ? mine.map(function (item) {
            return '<div class="fb-item">' +
              '<span class="fb-who">' + escapeHtml(item.who || "访客") + "</span>" +
              '<span class="fb-tag">' + escapeHtml(item.kind_label) + "</span>" +
              (item.message ? '<span class="fb-text">' + escapeHtml(item.message) + "</span>" : "") +
              '<span class="fb-meta">' +
                new Date(item.created_at * 1000).toLocaleString("zh-CN", { hour12: false }) +
                (item.device ? " · " + escapeHtml(item.device) : "") + "</span>" +
            "</div>";
          }).join("") : '<div class="fb-hint">还没有人反馈这条源，欢迎第一个</div>') + "</div>" +
        "</div></div>";
    }).join("");
  }

  function submitFeedback(url, kind, message, button) {
    if (button) button.disabled = true;
    return fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, kind: kind, message: message || "" })
    }).then(function (r) {
      return r.json().then(function (payload) { return { ok: r.ok, payload: payload }; });
    }).then(function (result) {
      if (!result.ok) { toast(result.payload.error || "提交失败"); return false; }
      toast("已收到，感谢反馈");
      if (currentChannel) loadFeedback(currentChannel);
      return true;
    }).catch(function () { toast("提交失败"); return false; })
      .then(function (ok) {
        if (button) button.disabled = false;
        return ok;
      });
  }

  function loadFeedback(channel) {
    return fetch("/api/feedback?limit=100&channel=" + encodeURIComponent(channel.key))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        feedbackState.summary = data.summary || {};
        feedbackState.items = data.items || [];
        feedbackState.you = data.you || "";
        renderStreams(channel);
      }).catch(function () { renderStreams(channel); });
  }

  function play(channel, url, forceProxy) {
    var video = el("player");
    el("modalTitle").textContent = channel.name;
    el("modal").classList.add("open");
    currentChannel = channel;

    feedbackState.summary = {};
    feedbackState.items = [];
    renderStreams(channel);
    loadFeedback(channel);
    remindOnce();
    trackPlay(channel);

    var stream = streamOf(channel, url || channel.url);
    var viaProxy = !stream.direct || forceProxy;
    var target = playable(stream, forceProxy);
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }

    loadHls(function () {
      if (window.Hls && window.Hls.isSupported()) {
        hlsInstance = new window.Hls({ maxBufferLength: 10 });
        hlsInstance.loadSource(target);
        hlsInstance.attachMedia(video);
        hlsInstance.on(window.Hls.Events.ERROR, function (_e, data) {
          if (!data.fatal) return;
          if (!viaProxy) {
            // 直连失败（多半是上游临时不给 CORS），退回本站中转再试一次
            toast("直连失败，改用本站中转…");
            play(channel, stream.url, true);
            return;
          }
          // 可能是中转配额用完了，去问一下真实原因
          fetch(target).then(function (r) {
            if (r.status === 429 || r.status === 503) {
              return r.json().then(function (payload) {
                toast(payload.error || "预览暂时不可用，请复制地址用 VLC 播放");
              });
            }
            toast("播放失败：" + data.details);
          }).catch(function () { toast("播放失败：" + data.details); });
        });
      } else {
        video.src = target;
      }
      video.play().catch(function () { /* 需要用户手势，忽略 */ });
      startSessionTimer(video);
    });
  }

  /* 播放上报：只报频道，身份由服务端按来源 IP 归并，用于后台的热门频道统计 */
  function trackPlay(channel) {
    try {
      var body = JSON.stringify({ event: "play", key: channel.key, name: channel.name });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/track", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/api/track", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: body, keepalive: true
        }).catch(function () {});
      }
    } catch (e) { /* 统计失败不影响播放 */ }
  }

  function startSessionTimer(video) {
    clearTimeout(sessionTimer);
    sessionTimer = setTimeout(function () {
      video.pause();
      toast("网页试看已暂停（" + PREVIEW_MINUTES + " 分钟上限）。继续观看请用 VLC 订阅，点「怎么用？」看步骤");
    }, PREVIEW_MINUTES * 60000);
  }

  function closeModal() {
    clearTimeout(sessionTimer);
    el("modal").classList.remove("open");
    var video = el("player");
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
  }

  // ------------------------------------------------------ 专用播放器引导
  /* 浏览器放直播先天吃亏：混合内容与跨域限制、切台慢、后台标签页会被节流，
     还要占本站中转配额。所以主动把用户引到 VLC 这类专用播放器上。 */
  function guideHtml() {
    var sub = subscriptionUrl();
    return '<div class="guide">' +
      '<div class="why"><b>为什么推荐专用播放器？</b><ul>' +
        "<li>浏览器对直播流限制多：http 源受混合内容拦截，跨域也常被挡，只能经本站中转</li>" +
        "<li>专用播放器直连源站，起播快、切台快，硬件解码更省电，画面也更稳</li>" +
        "<li>支持后台播放、投屏、记忆频道；网页试看 10 分钟会自动暂停</li>" +
        "<li>不占用本站中转流量，大家都更顺畅</li>" +
      "</ul></div>" +

      "<h4>订阅地址（随当前筛选变化）</h4>" +
      '<div class="sub-url"><code id="guideUrl">' + escapeHtml(sub) + "</code>" +
        '<button class="primary" id="guideCopy">复制</button></div>' +

      "<h4>Windows / macOS —— VLC</h4><ol>" +
        "<li>菜单「媒体 → 打开网络串流」（macOS 是「文件 → 打开网络」）</li>" +
        "<li>粘贴上面的订阅地址，点播放</li>" +
        "<li>或者直接双击下载好的 <code>.m3u</code> 文件，VLC 会列出全部频道</li>" +
      "</ol>" +

      "<h4>iPhone / iPad</h4><ol>" +
        "<li>App Store 安装 <b>VLC for Mobile</b>（或 nPlayer、APTV）</li>" +
        "<li>VLC 里「网络 → 打开网络串流」，粘贴订阅地址</li>" +
        "<li>也可以在本页点「下载 m3u」，然后选择用 VLC 打开</li>" +
      "</ol>" +

      "<h4>Android / 电视盒子</h4><ol>" +
        "<li>安装 <b>VLC for Android</b>、Kodi、TiviMate 或「我的IPTV」</li>" +
        "<li>选择「添加网络播放列表 / 远程 m3u」，填入订阅地址</li>" +
        "<li>电视端建议用遥控器友好的 TiviMate、Kodi</li>" +
      "</ol>" +

      "<h4>小提示</h4><ul>" +
        "<li>订阅地址支持筛选参数，例如只要央视 1080p：" +
          "<code>/playlist.m3u?group=央视频道&min_height=1080</code></li>" +
        "<li>播放列表每 4 小时自动更新，播放器里重新加载一次即可拿到最新源</li>" +
        "<li>某条源卡顿或打不开，欢迎在频道的「反馈这条源」里点一下告诉我们</li>" +
      "</ul></div>";
  }

  function openGuide() {
    el("guideBody").innerHTML = guideHtml();
    el("guideModal").classList.add("open");
    el("guideCopy").onclick = function () { copy(subscriptionUrl()); };
  }

  function bindPlayerTip() {
    el("tipCopy").onclick = function () { copy(subscriptionUrl()); };
    el("tipDownload").onclick = function () {
      location.href = state.static ? "playlist.m3u" : "/playlist.m3u" + params();
    };
    el("tipGuide").onclick = openGuide;
    el("guideClose").onclick = function () { el("guideModal").classList.remove("open"); };
    el("guideModal").addEventListener("click", function (event) {
      if (event.target === el("guideModal")) el("guideModal").classList.remove("open");
    });
  }

  /* 第一次点播放时提醒一次，之后不再打扰 */
  function remindOnce() {
    if (localStorage.getItem("iptvhub_player_hint")) return;
    localStorage.setItem("iptvhub_player_hint", "1");
    setTimeout(function () {
      toast("网页播放仅供试看，长期观看建议用 VLC 订阅，点上方「怎么用？」查看");
    }, 1200);
  }

  // ---------------------------------------------------------------- 公告
  /* 节日祝福按日期区间自动上下线（见 config/notices.json），
     欢迎弹窗每条公告只弹一次，横幅在有效期内一直挂着。 */
  function loadNotice() {
    fetch("/api/notice").then(function (r) { return r.json(); }).then(function (data) {
      var notice = data.notice;
      if (!notice) return;
      renderNoticeBar(notice);
      var seenKey = "iptvhub_notice_" + notice.id;
      if (!localStorage.getItem(seenKey)) {
        showWelcome(notice, seenKey);
      }
    }).catch(function () { /* 公告不是关键功能，失败就当没有 */ });
  }

  function renderNoticeBar(notice) {
    el("noticeBar").innerHTML =
      '<div class="notice ' + escapeHtml(notice.style || "info") + '">' +
        '<span class="notice-emoji">' + escapeHtml(notice.emoji || "🎉") + "</span>" +
        '<span class="notice-title">' + escapeHtml(notice.title || "") + "</span>" +
        (notice.subtitle
          ? '<span class="notice-sub">' + escapeHtml(notice.subtitle) + "</span>" : "") +
        '<button class="link notice-more">查看详情</button>' +
      "</div>";
    var more = el("noticeBar").querySelector(".notice-more");
    if (more) {
      more.onclick = function () { showWelcome(notice, "iptvhub_notice_" + notice.id); };
    }
  }

  function showWelcome(notice, seenKey) {
    el("welcomeBody").innerHTML =
      '<div class="welcome-emoji">' + escapeHtml(notice.emoji || "🎉") + "</div>" +
      "<h2>" + escapeHtml(notice.title || "") + "</h2>" +
      (notice.subtitle ? '<p class="welcome-sub">' + escapeHtml(notice.subtitle) + "</p>" : "") +
      "<ul>" + (notice.lines || []).map(function (line) {
        return "<li>" + escapeHtml(line) + "</li>";
      }).join("") + "</ul>" +
      '<button class="primary" id="welcomeEnter">' +
      escapeHtml(notice.button || "进入") + "</button>";
    el("welcomeBox").classList.add(escapeHtml(notice.style || "info"));
    el("welcomeModal").classList.add("open");
    function dismiss() {
      localStorage.setItem(seenKey, "1");
      el("welcomeModal").classList.remove("open");
    }
    el("welcomeEnter").onclick = dismiss;
    el("welcomeModal").onclick = function (event) {
      if (event.target === el("welcomeModal")) dismiss();
    };
    document.addEventListener("keydown", function onEsc(event) {
      if (event.key === "Escape") {
        dismiss();
        document.removeEventListener("keydown", onEsc);
      }
    });
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
      var guideLink = event.target.closest("[data-guide]");
      if (guideLink) {
        event.preventDefault();
        openGuide();
        return;
      }
      var button = event.target.closest("button");
      if (!button) return;

      if (button.dataset.switch) {
        if (currentChannel) play(currentChannel, button.dataset.switch);
        return;
      }

      if (button.dataset.fb) {
        feedbackState.openUrl = feedbackState.openUrl === button.dataset.fb
          ? "" : button.dataset.fb;
        if (currentChannel) renderStreams(currentChannel);
        return;
      }

      // 点一下类型就直接提交，顺带捎上已经填好的补充说明
      if (button.dataset.kind) {
        var panel = button.closest(".fb-panel");
        var note = panel.querySelector(".fb-msg");
        submitFeedback(button.dataset.url, button.dataset.kind,
                       note ? note.value : "", button).then(function (ok) {
          if (ok && note) note.value = "";
        });
        return;
      }

      if (button.dataset.send) {
        var box = button.closest(".fb-panel").querySelector(".fb-msg");
        var text = box ? box.value.trim() : "";
        if (!text) {
          toast("先写点说明，或直接点上面的选项");
          return;
        }
        submitFeedback(button.dataset.send, "other", text, button).then(function (ok) {
          if (ok && box) box.value = "";
        });
      }
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
  bindPlayerTip();
  loadNotice();
  loadStats();
  loadChannels();
  setInterval(loadStats, 60000);
})();
