(function () {
  "use strict";

  function ready(callback) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", callback, { once: true });
    else callback();
  }

  ready(function () {
    document.querySelectorAll("[data-confirm]").forEach(function (element) {
      element.addEventListener("click", function (event) {
        if (!element.form) return;
        event.preventDefault();
        var dialog = document.createElement("dialog");
        dialog.setAttribute("aria-label", "确认操作");
        dialog.style.cssText = "max-width:440px;padding:28px;border:1px solid #ddd;border-radius:12px;";
        var message = document.createElement("p");
        message.textContent = element.dataset.confirm;
        var actions = document.createElement("div");
        actions.className = "actions";
        var cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "button button--ghost";
        cancel.textContent = "取消";
        var confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "button button--primary";
        confirm.textContent = "确认继续";
        cancel.addEventListener("click", function () { dialog.close(); });
        dialog.addEventListener("close", function () { dialog.remove(); element.focus(); });
        confirm.addEventListener("click", function () {
          dialog.close();
          element.form.requestSubmit(element);
        });
        actions.append(cancel, confirm);
        dialog.append(message, actions);
        document.body.append(dialog);
        dialog.showModal();
        cancel.focus();
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
