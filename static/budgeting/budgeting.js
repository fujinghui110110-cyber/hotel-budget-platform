(function () {
  "use strict";

  function ready(callback) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", callback, { once: true });
    else callback();
  }

  ready(function () {
    document.querySelectorAll("[data-confirm]").forEach(function (element) {
      element.addEventListener("click", function (event) {
        if (!window.confirm(element.dataset.confirm)) event.preventDefault();
      });
    });

    document.querySelectorAll("[data-busy-text]").forEach(function (button) {
      if (!button.form) return;
      button.form.addEventListener("submit", function () {
        button.disabled = true;
        button.textContent = button.dataset.busyText;
      });
    });

    document.querySelectorAll("[data-status-url]").forEach(function (container) {
      var output = container.querySelector("[data-upload-status]");
      if (!container.dataset.statusUrl || !output) return;
      var refresh = function () {
        fetch(container.dataset.statusUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (response) { return response.ok ? response.json() : null; })
          .then(function (payload) {
            if (!payload || !payload.status) return;
            output.textContent = payload.status_label || payload.status;
            if (["validated", "approved", "rejected", "failed"].indexOf(payload.status) >= 0) {
              window.clearInterval(timer);
              window.location.reload();
            }
          })
          .catch(function () { return undefined; });
      };
      var timer = window.setInterval(refresh, 5000);
      refresh();
    });
  });
}());
