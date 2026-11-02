(function () {
  "use strict";

  var tag = document.currentScript;
  var uid = tag ? tag.getAttribute("data-uid") : null;
  var sessionId = tag ? tag.getAttribute("data-session") : null;

  if (!uid || !sessionId) return;

  var redirecting = false;
  var nativeFetch = window.fetch;

  function goToLogin() {
    if (redirecting) return;

    redirecting = true;
    window.location.replace("/login");
  }

  function addIdentityHeader(headers) {
    var result = new Headers(headers || {});
    result.set("X-Page-Uid", uid);
    result.set("X-Session-Id", sessionId);
    return result;
  }

  if (nativeFetch) {
    window.fetch = function (input, init) {
      init = init || {};

      var requestUrl = input instanceof Request ? input.url : String(input);

      var local;

      try {
        local =
          new URL(requestUrl, window.location.href).origin ===
          window.location.origin;
      } catch (e) {
        local = false;
      }

      if (local) {
        init.headers = addIdentityHeader(
          init.headers ||
            (input instanceof Request ? input.headers : undefined),
        );
      }

      return nativeFetch.call(window, input, init).then(function (response) {
        if (
          local &&
          response.status === 409 &&
          response.headers.get("X-Account-Changed") === "1"
        ) {
          goToLogin();
        }

        if (local && response.status === 401) {
          goToLogin();
        }

        return response;
      });
    };
  }

  var nativeOpen = XMLHttpRequest.prototype.open;
  var nativeSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__accountLocal = false;

    try {
      this.__accountLocal =
        new URL(String(url), window.location.href).origin ===
        window.location.origin;
    } catch (e) {}

    return nativeOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    if (this.__accountLocal) {
      try {
        this.setRequestHeader("X-Page-Uid", uid);
        this.setRequestHeader("X-Session-Id", sessionId);
      } catch (e) {}
    }

    return nativeSend.apply(this, arguments);
  };

  function checkSession() {
    if (redirecting) return;

    nativeFetch
      .call(window, "/api/whoami", {
        cache: "no-store",
        headers: {
          "X-Page-Uid": uid,
          "X-Session-Id": sessionId,
        },
      })
      .then(function (response) {
        if (!response.ok) {
          goToLogin();
          return null;
        }

        return response.json();
      })
      .then(function (data) {
        if (
          data &&
          (!data.success ||
            String(data.user || "") === "" ||
            data.session_id !== sessionId)
        ) {
          goToLogin();
        }
      })
      .catch(function () {});
  }

  window.addEventListener("focus", checkSession);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      checkSession();
    }
  });

  setInterval(checkSession, 15000);

  var realIo;

  Object.defineProperty(window, "io", {
    configurable: true,

    get: function () {
      return realIo;
    },

    set: function (library) {
      if (typeof library !== "function") {
        realIo = library;
        return;
      }

      var wrapped = function (a, b) {
        var hasUrl = typeof a === "string";
        var options = Object.assign({}, (hasUrl ? b : a) || {});

        return hasUrl ? library(a, options) : library(options);
      };

      Object.keys(library).forEach(function (key) {
        wrapped[key] = library[key];
      });

      wrapped.connect = wrapped;
      realIo = wrapped;
    },
  });

  window.addEventListener("load", function () {
    function startSocket() {
      if (typeof io !== "function") return;

      var socket = io({ transports: ["polling"] });

      socket.on("connect", function () {
        socket.emit("join_slot");
      });

      socket.on("logged_out", function (data) {
        if (data && data.session_id === sessionId) {
          goToLogin();
        }
      });
    }

    if (typeof io === "function") {
      startSocket();
      return;
    }

    var script = document.createElement("script");

    script.src =
      "https://cdn.jsdelivr.net/npm/socket.io-client@4.8.1/dist/socket.io.min.js";

    script.onload = startSocket;
    document.head.appendChild(script);
  });
})();
