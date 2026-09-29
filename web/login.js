(function () {
  "use strict";

  var form = document.getElementById("loginForm");
  var error = document.getElementById("loginError");
  var button = document.getElementById("loginBtn");

  function nextUrl() {
    var match = /[?&]next=([^&]+)/.exec(location.search);
    return match ? decodeURIComponent(match[1]) : "/";
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    error.textContent = "";
    button.disabled = true;
    button.textContent = "登录中…";

    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value
      })
    }).then(function (response) {
      return response.json().then(function (payload) {
        return { ok: response.ok, payload: payload };
      });
    }).then(function (result) {
      if (!result.ok) {
        error.textContent = result.payload.error || "登录失败";
        return;
      }
      var target = nextUrl();
      // 普通用户点不开后台，直接送回首页
      if (target.indexOf("/admin") === 0 && !result.payload.user.is_admin) target = "/";
      location.href = target;
    }).catch(function () {
      error.textContent = "网络异常，请重试";
    }).then(function () {
      button.disabled = false;
      button.textContent = "登录";
    });
  });

  document.getElementById("username").focus();
})();
