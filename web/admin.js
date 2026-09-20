/* IPTV-Hub 管理后台。所有请求都带 X-Admin-Token；令牌存在 localStorage。 */
(function () {
  "use strict";

  var TOKEN_KEY = "iptvhub_admin_token";
  var S = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    tab: "overview",
    logSeq: 0,
    sources: [],
    errors: [],
    streams: { offset: 0, limit: 100, total: 0 },
    config: null,
    editable: null,
    polling: null,
    loaded: {}
  };

  function el(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function toast(message) {
    var node = el("toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(node._t);
    node._t = setTimeout(function () { node.classList.remove("show"); }, 2200);
  }
  function kbps(value) {
    if (!value) return "—";
    return value >= 1000 ? (value / 1000).toFixed(1) + " Mbps" : Math.round(value) + " kbps";
  }
  function timeOf(seconds) {
    if (!seconds) return "—";
    return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
  }
  function duration(seconds) {
    if (!seconds) return "0s";
    if (seconds < 60) return seconds + "s";
    return Math.floor(seconds / 60) + "m" + String(seconds % 60).padStart(2, "0") + "s";
  }

  // ------------------------------------------------------------------ 请求
  function api(path, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      headers: { "X-Admin-Token": S.token }
    };
    if (options.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch("/api/admin" + path, init).then(function (response) {
      if (response.status === 401) {
        showAuth("令牌无效，请重新输入");
        throw new Error("unauthorized");
      }
      return response.json().then(function (payload) {
        if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
        return payload;
      });
    });
  }

  // ------------------------------------------------------------------ 鉴权
  function showAuth(message) {
    el("authError").textContent = message || "";
    el("authModal").classList.add("open");
    el("tokenInput").focus();
    stopPolling();
  }
  function hideAuth() { el("authModal").classList.remove("open"); }

  function tryToken(value) {
    S.token = value.trim();
    return api("/summary").then(function (data) {
      localStorage.setItem(TOKEN_KEY, S.token);
      hideAuth();
      renderSummary(data);
      startPolling();
      return data;
    });
  }

  // ------------------------------------------------------------- 概览渲染
  function renderSummary(data) {
    var stats = data.stats || {};
    el("tChannels").textContent = data.channel_count || 0;
    el("tStreams").textContent = (stats.streams_alive || 0) + " / " + (stats.streams_total || 0);
    el("tLatency").textContent = Math.round(stats.avg_ttfb_ms || 0) + " ms";
    el("tKbps").textContent = kbps(stats.avg_kbps || 0);
    el("tVersion").textContent = data.generated_at || "—";
    el("tAuto").textContent = data.auto_update
      ? "开启 · 每 " + data.update_interval_hours + " 小时" : "已关闭";
    el("serverTime").textContent = "服务器时间 " + (data.server_time || "");

    el("runsBody").innerHTML = (data.runs || []).map(function (run) {
      return "<tr><td>" + run.id + "</td><td>" + timeOf(run.started_at) + "</td><td>" +
        (run.candidates || 0) + "</td><td>" + (run.probed || 0) + "</td><td>" +
        (run.alive || 0) + "</td><td>" + (run.channels || 0) + "</td><td>" +
        esc(run.note || (run.finished_at ? "" : "进行中")) + "</td></tr>";
    }).join("");

    S.errors = data.errors || [];
    var maxCount = S.errors.reduce(function (a, b) { return Math.max(a, b.count); }, 1);
    el("errorsBody").innerHTML = S.errors.map(function (item) {
      var pct = Math.round(100 * item.count / maxCount);
      return "<tr><td><code>" + esc(item.error) + "</code></td><td>" + item.count +
        '</td><td><div class="bar bad" style="width:80px"><i style="width:' + pct +
        '%"></i></div></td></tr>';
    }).join("");

    S.sources = data.sources || [];
    if (S.tab === "sources") renderSources();
    applyRun(data.run || {});
  }

  function applyRun(run) {
    var percent = run.percent || 0;
    el("progressBar").style.width = percent + "%";
    var phases = { idle: "空闲", collect: "采集上游清单", probe: "探测中",
                   export: "评分与导出", done: "已完成", error: "出错" };
    el("progressPhase").textContent = (phases[run.phase] || run.phase || "空闲") +
      (run.message ? " · " + run.message : "");
    el("progressCount").textContent = run.total
      ? run.done + " / " + run.total + "（可用 " + run.alive + "，" + percent + "%）" : "";
    el("progressElapsed").textContent = run.elapsed ? "已用 " + duration(run.elapsed) : "";
    document.querySelectorAll("[data-run]").forEach(function (button) {
      button.disabled = !!run.running;
    });
  }

  function appendLogs(logs) {
    if (!logs || !logs.length) return;
    var box = el("logs");
    if (box.textContent === "等待日志…") box.textContent = "";
    var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    var html = logs.map(function (row) {
      var stamp = new Date(row.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
      var cls = row.level === "ERROR" ? "err" : (row.level === "WARNING" ? "warn" : "");
      return '<span class="' + cls + '">' + stamp + " " + esc(row.message) + "</span>";
    }).join("\n");
    box.insertAdjacentHTML("beforeend", (box.innerHTML ? "\n" : "") + html);
    S.logSeq = logs[logs.length - 1].seq;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function poll() {
    api("/progress?after=" + S.logSeq).then(function (data) {
      applyRun(data.run || {});
      appendLogs(data.logs);
      if (data.run && !data.run.running && S.wasRunning) {
        S.wasRunning = false;
        toast("更新完成：" + (data.run.message || ""));
        refreshCurrentTab(true);
      }
      if (data.run && data.run.running) S.wasRunning = true;
      schedule(data.run && data.run.running ? 1500 : 5000);
    }).catch(function () { schedule(8000); });
  }
  function schedule(delay) {
    stopPolling();
    S.polling = setTimeout(poll, delay);
  }
  function startPolling() { schedule(300); }
  function stopPolling() { if (S.polling) { clearTimeout(S.polling); S.polling = null; } }

  // ------------------------------------------------------------- 上游源
  function renderSources() {
    el("sourcesBody").innerHTML = S.sources.map(function (item, index) {
      var stats = item.stats || { candidates: 0, alive: 0 };
      return "<tr data-i=" + index + ">" +
        '<td><input type="checkbox" data-f="enabled"' + (item.enabled ? " checked" : "") + "></td>" +
        '<td><input type="text" data-f="name" value="' + esc(item.name) + '"></td>' +
        '<td><input type="text" data-f="url" value="' + esc(item.url) + '"></td>' +
        '<td><select data-f="type">' +
          ["auto", "m3u", "txt"].map(function (t) {
            return '<option value="' + t + '"' + (item.type === t ? " selected" : "") + ">" + t + "</option>";
          }).join("") + "</select></td>" +
        '<td><input type="number" step="0.1" min="0.1" max="2" data-f="weight" value="' +
          item.weight + '"></td>' +
        '<td class="num">' + stats.candidates + " / <b>" + stats.alive + "</b></td>" +
        '<td><div class="row-actions"><button data-test="' + index + '">测试</button>' +
          '<button data-del="' + index + '">删除</button></div></td></tr>';
    }).join("");
  }

  function collectSources() {
    var rows = [];
    el("sourcesBody").querySelectorAll("tr").forEach(function (tr) {
      var item = {};
      tr.querySelectorAll("[data-f]").forEach(function (input) {
        var field = input.getAttribute("data-f");
        item[field] = input.type === "checkbox" ? input.checked : input.value;
      });
      if (item.url) rows.push(item);
    });
    return rows;
  }

  // ------------------------------------------------------------- 源明细
  function loadStreams(reset) {
    if (reset) S.streams.offset = 0;
    var params = new URLSearchParams({
      q: el("sQ").value.trim(), group: el("sGroup").value, alive: el("sAlive").value,
      error: el("sError").value, order: el("sOrder").value,
      limit: S.streams.limit, offset: S.streams.offset
    });
    api("/streams?" + params.toString()).then(function (data) {
      S.streams.total = data.total;
      if (el("sGroup").options.length <= 1) {
        el("sGroup").insertAdjacentHTML("beforeend", (data.groups || []).map(function (g) {
          return '<option value="' + esc(g) + '">' + esc(g) + "</option>";
        }).join(""));
      }
      if (el("sError").options.length <= 1 && S.errors.length) {
        el("sError").insertAdjacentHTML("beforeend", S.errors.map(function (e) {
          return '<option value="' + esc(e.error) + '">' + esc(e.error) + " (" + e.count + ")</option>";
        }).join(""));
      }
      el("streamsBody").innerHTML = data.rows.map(function (row) {
        var uptime = row.checks ? Math.round(100 * row.successes / row.checks) : 0;
        return "<tr>" +
          "<td>" + esc(row.display_name) + "</td>" +
          '<td class="hide-sm">' + esc(row.group_title) + "</td>" +
          '<td class="num">' + (row.score || 0).toFixed(2) + "</td>" +
          '<td class="' + (row.alive ? "state-ok" : "state-bad") + '">' +
            (row.alive ? "可用" : esc(row.last_error || "失效")) + "</td>" +
          '<td class="num hide-sm">' + (row.ttfb_ms ? Math.round(row.ttfb_ms) + " ms" : "—") + "</td>" +
          '<td class="num hide-sm">' + kbps(row.kbps) + "</td>" +
          '<td class="num hide-sm">' + esc(row.resolution || "—") + "</td>" +
          '<td class="num hide-sm">' + row.checks + " / " + uptime + "%</td>" +
          '<td class="hide-sm"><code>' + esc(row.host) + "</code></td>" +
          '<td><div class="row-actions">' +
            '<button data-hist="' + esc(row.url) + '">历史</button>' +
            '<button data-probe="' + esc(row.url) + '">重测</button>' +
            '<button data-rm="' + esc(row.url) + '">删除</button>' +
          "</div></td></tr>";
      }).join("");
      var from = S.streams.offset + 1;
      var to = S.streams.offset + data.rows.length;
      el("pageInfo").textContent = data.total ? (from + "–" + to + " / " + data.total) : "无结果";
      el("prevPage").disabled = S.streams.offset <= 0;
      el("nextPage").disabled = to >= data.total;
    }).catch(function (err) { toast("查询失败：" + err.message); });
  }

  function showDetail(title, payload) {
    el("detailTitle").textContent = title;
    el("detailBody").textContent = typeof payload === "string"
      ? payload : JSON.stringify(payload, null, 2);
    el("detailModal").classList.add("open");
  }

  // ------------------------------------------------------------- 参数
  var FIELD_LABELS = {
    concurrency: "全局并发探测数", per_host_concurrency: "单主机并发上限",
    connect_timeout: "连接超时(秒)", probe_timeout: "单条探测总超时(秒)",
    probe_bytes: "测速读取字节上限", probe_seconds: "测速读取时长(秒)",
    min_bytes: "判定有码流的最小字节", manifest_max_bytes: "manifest 读取上限",
    probe_retries: "失败重试次数", host_failure_limit: "主机熔断阈值",
    ewma_alpha: "EWMA 系数", prune_after_days: "清理：多少天未成功",
    prune_fail_streak: "清理：连续失败次数", recheck_dead_after_hours: "失效冷却(小时)",
    max_backups_per_channel: "每频道备用源数", max_per_host_per_channel: "同频道同主机上限",
    min_score: "入选最低评分", fetch_timeout: "上游清单抓取超时(秒)",
    stability: "稳定性", speed: "吞吐", quality: "画质", latency: "延迟",
    https_bonus: "HTTPS 加成", ipv4_bonus: "IPv4 加成",
    epg_url: "EPG 节目单地址", site_url: "对外站点地址", user_agent: "探测 User-Agent"
  };
  var GROUPS = [
    ["探测", ["concurrency", "per_host_concurrency", "connect_timeout", "probe_timeout",
              "probe_bytes", "probe_seconds", "min_bytes", "manifest_max_bytes",
              "probe_retries", "host_failure_limit", "fetch_timeout"]],
    ["评分与选优", ["ewma_alpha", "min_score", "max_backups_per_channel",
                   "max_per_host_per_channel"]],
    ["清理策略", ["prune_after_days", "prune_fail_streak", "recheck_dead_after_hours"]]
  ];

  function renderSettings(data) {
    S.config = data.config || {};
    S.editable = data.editable || {};
    var html = GROUPS.map(function (group) {
      var fields = group[1].filter(function (key) {
        return S.editable.numbers && key in S.editable.numbers;
      }).map(function (key) {
        var range = S.editable.numbers[key];
        return '<div class="field"><label>' + (FIELD_LABELS[key] || key) +
          '<div class="hint">' + range[0] + " ~ " + range[1] + "</div></label>" +
          '<input type="number" step="any" data-k="' + key + '" value="' +
          (S.config[key] !== undefined ? S.config[key] : "") + '"></div>';
      }).join("");
      return "<fieldset><legend>" + group[0] + "</legend>" + fields + "</fieldset>";
    }).join("");

    var weights = S.config.weights || {};
    html += "<fieldset><legend>评分权重</legend>" +
      (S.editable.weights || []).map(function (key) {
        return '<div class="field"><label>' + (FIELD_LABELS[key] || key) + "</label>" +
          '<input type="number" step="0.01" min="0" max="1" data-w="' + key + '" value="' +
          (weights[key] !== undefined ? weights[key] : 0) + '"></div>';
      }).join("") + "</fieldset>";

    var server = S.config.server || {};
    html += "<fieldset><legend>服务（需重启生效）</legend>" +
      '<div class="field"><label>自动更新</label><select data-s="auto_update">' +
        '<option value="1"' + (server.auto_update ? " selected" : "") + ">开启</option>" +
        '<option value="0"' + (server.auto_update ? "" : " selected") + ">关闭</option></select></div>" +
      '<div class="field"><label>更新间隔(小时)</label>' +
        '<input type="number" min="1" max="72" data-s="update_interval_hours" value="' +
        (server.update_interval_hours || 4) + '"></div>' +
      '<div class="field full"><label>管理令牌（留空则沿用 data/admin_token）</label>' +
        '<input type="text" data-s="admin_token" value="' + esc(server.admin_token || "") +
        '"></div></fieldset>';

    html += "<fieldset><legend>其它</legend>" +
      (S.editable.strings || []).map(function (key) {
        return '<div class="field full"><label>' + (FIELD_LABELS[key] || key) + "</label>" +
          '<input type="text" data-t="' + key + '" value="' + esc(S.config[key] || "") + '"></div>';
      }).join("") + "</fieldset>";

    el("settingsForm").innerHTML = html;
  }

  function collectConfig() {
    var patch = { weights: {}, server: {} };
    el("settingsForm").querySelectorAll("[data-k]").forEach(function (input) {
      if (input.value !== "") patch[input.getAttribute("data-k")] = Number(input.value);
    });
    el("settingsForm").querySelectorAll("[data-w]").forEach(function (input) {
      patch.weights[input.getAttribute("data-w")] = Number(input.value);
    });
    el("settingsForm").querySelectorAll("[data-t]").forEach(function (input) {
      patch[input.getAttribute("data-t")] = input.value;
    });
    el("settingsForm").querySelectorAll("[data-s]").forEach(function (input) {
      var key = input.getAttribute("data-s");
      var value = input.value;
      if (key === "auto_update") value = value === "1";
      else if (key === "update_interval_hours") value = Number(value);
      if (key === "admin_token" && !value) return;   // 留空表示不改
      patch.server[key] = value;
    });
    return patch;
  }

  // ------------------------------------------------------------- 标签页
  function switchTab(name) {
    S.tab = name;
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".tabpage").forEach(function (page) {
      page.classList.toggle("hidden", page.id !== "page-" + name);
    });
    refreshCurrentTab();
  }

  function refreshCurrentTab(force) {
    if (S.tab === "sources") {
      if (force || !S.loaded.sources) {
        api("/sources").then(function (data) {
          S.sources = data.sources || [];
          S.loaded.sources = true;
          renderSources();
        });
      }
    } else if (S.tab === "streams") {
      if (force || !S.loaded.streams) { S.loaded.streams = true; loadStreams(true); }
    } else if (S.tab === "settings") {
      if (force || !S.loaded.settings) {
        api("/config").then(function (data) { S.loaded.settings = true; renderSettings(data); });
      }
    } else if (force) {
      api("/summary").then(renderSummary);
    }
  }

  // ------------------------------------------------------------- 事件绑定
  function bind() {
    el("tabs").addEventListener("click", function (event) {
      var tab = event.target.closest(".tab");
      if (tab) switchTab(tab.getAttribute("data-tab"));
    });

    el("tokenSave").onclick = function () {
      tryToken(el("tokenInput").value).catch(function () {});
    };
    el("tokenInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter") el("tokenSave").click();
    });
    el("lockBtn").onclick = function () {
      localStorage.removeItem(TOKEN_KEY);
      S.token = "";
      showAuth("已退出");
    };

    document.querySelectorAll("[data-run]").forEach(function (button) {
      button.onclick = function () {
        var mode = button.getAttribute("data-run");
        var body = { recheck_all: mode === "recheck", skip_probe: mode === "collect",
                     limit: mode === "limit" ? 200 : 0 };
        api("/update", { method: "POST", body: body }).then(function (data) {
          toast(data.started ? "已开始更新" : "已有任务在运行");
          S.wasRunning = true;
          startPolling();
        }).catch(function (err) { toast("触发失败：" + err.message); });
      };
    });

    document.querySelectorAll("[data-act]").forEach(function (button) {
      button.onclick = function () {
        var act = button.getAttribute("data-act");
        if (act === "prune" && !confirm("将删除长期失效的源（有上游收录的会在下轮重新加入），继续？")) return;
        button.disabled = true;
        api("/" + act, { method: "POST", body: {} }).then(function (data) {
          toast(JSON.stringify(data));
          refreshCurrentTab(true);
        }).catch(function (err) { toast("失败：" + err.message); })
          .then(function () { button.disabled = false; });
      };
    });

    el("addSource").onclick = function () {
      S.sources.push({ name: "", url: "", type: "auto", enabled: true, weight: 1.0,
                       stats: { candidates: 0, alive: 0 } });
      renderSources();
    };
    el("saveSources").onclick = function () {
      api("/sources", { method: "POST", body: { sources: collectSources() } })
        .then(function (data) {
          toast("已保存 " + data.saved + " 个源（启用 " + data.enabled + "）");
          refreshCurrentTab(true);
        }).catch(function (err) { toast("保存失败：" + err.message); });
    };
    el("sourcesBody").addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      var row = button.closest("tr");
      if (button.dataset.del) {
        S.sources = collectSources();
        S.sources.splice(Number(button.dataset.del), 1);
        renderSources();
        toast("已移除，记得点保存");
      } else if (button.dataset.test) {
        var url = row.querySelector('[data-f="url"]').value;
        var type = row.querySelector('[data-f="type"]').value;
        button.disabled = true;
        el("sourceTestOut").classList.remove("hidden");
        el("sourceTestOut").textContent = "正在抓取 " + url + " …";
        api("/sources/test", { method: "POST", body: { url: url, type: type } })
          .then(function (data) {
            el("sourceTestOut").textContent =
              "状态 " + data.status + " · 耗时 " + data.elapsed_ms + "ms · 解析 " +
              data.entries + " 条 · 其中新地址 " + (data.new_urls || 0) + " 条\n" +
              (data.samples || []).map(function (s) {
                return "  " + s.name + "  [" + s.group + "]  " + s.url;
              }).join("\n");
          }).catch(function (err) { el("sourceTestOut").textContent = "失败：" + err.message; })
          .then(function () { button.disabled = false; });
      }
    });

    el("sSearch").onclick = function () { loadStreams(true); };
    el("sQ").addEventListener("keydown", function (e) { if (e.key === "Enter") loadStreams(true); });
    ["sGroup", "sAlive", "sError", "sOrder"].forEach(function (id) {
      el(id).addEventListener("change", function () { loadStreams(true); });
    });
    el("prevPage").onclick = function () {
      S.streams.offset = Math.max(0, S.streams.offset - S.streams.limit);
      loadStreams(false);
    };
    el("nextPage").onclick = function () {
      S.streams.offset += S.streams.limit;
      loadStreams(false);
    };

    el("streamsBody").addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.hist) {
        api("/history?url=" + encodeURIComponent(button.dataset.hist)).then(function (data) {
          var lines = (data.checks || []).map(function (c) {
            return new Date(c.ts * 1000).toLocaleString("zh-CN", { hour12: false }) +
              (c.ok ? "  可用" : "  失败 " + c.error) +
              (c.ok ? "  " + Math.round(c.ttfb_ms) + "ms  " + kbps(c.kbps) : "");
          });
          showDetail("探测历史（最近 " + lines.length + " 次）",
                     button.dataset.hist + "\n\n" + lines.join("\n"));
        });
      } else if (button.dataset.probe) {
        button.disabled = true;
        toast("正在实时探测…");
        api("/probe", { method: "POST", body: { url: button.dataset.probe } })
          .then(function (data) { showDetail("实时探测结果", data); })
          .catch(function (err) { toast("探测失败：" + err.message); })
          .then(function () { button.disabled = false; });
      } else if (button.dataset.rm) {
        if (!confirm("从库中删除该源？若上游仍收录，下轮会重新加入。")) return;
        api("/stream/delete", { method: "POST", body: { url: button.dataset.rm } })
          .then(function () { toast("已删除"); loadStreams(false); });
      }
    });

    el("saveConfig").onclick = function () {
      api("/config", { method: "POST", body: { config: collectConfig() } })
        .then(function (data) {
          el("configOut").classList.remove("hidden");
          el("configOut").textContent =
            "已保存：" + Object.keys(data.applied || {}).join(", ") +
            (Object.keys(data.rejected || {}).length
              ? "\n被拒绝：" + JSON.stringify(data.rejected, null, 2) : "") +
            "\n" + (data.note || "");
          toast("参数已保存");
        }).catch(function (err) { toast("保存失败：" + err.message); });
    };

    el("detailClose").onclick = function () { el("detailModal").classList.remove("open"); };
    el("detailModal").addEventListener("click", function (event) {
      if (event.target === el("detailModal")) el("detailModal").classList.remove("open");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") el("detailModal").classList.remove("open");
    });
  }

  bind();
  if (S.token) {
    tryToken(S.token).catch(function () { showAuth("令牌无效，请重新输入"); });
  } else {
    showAuth();
  }
})();
