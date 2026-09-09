(() => {
    "use strict";

    const onReady = (callback) => {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", callback, { once: true });
        else callback();
    };

    onReady(() => {
        document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
            const input = document.getElementById(toggle.dataset.passwordToggle);
            if (!input) return;
            toggle.addEventListener("click", () => {
                const visible = input.type === "text";
                input.type = visible ? "password" : "text";
                toggle.textContent = visible ? "显示" : "隐藏";
                toggle.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
            });
        });

        document.querySelectorAll("[data-dropzone]").forEach((zone) => {
            const input = zone.querySelector("[data-file-input]");
            const filename = zone.querySelector("[data-file-name]");
            if (!input) return;
            const updateFilename = () => {
                filename.textContent = input.files && input.files.length ? input.files[0].name : "尚未选择文件";
            };
            input.addEventListener("change", updateFilename);
            ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
                event.preventDefault();
                zone.classList.add("is-dragover");
            }));
            ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
                event.preventDefault();
                zone.classList.remove("is-dragover");
            }));
            zone.addEventListener("drop", (event) => {
                if (!event.dataTransfer.files.length) return;
                input.files = event.dataTransfer.files;
                updateFilename();
            });
        });

        document.querySelectorAll("[data-confirm]").forEach((element) => {
            element.addEventListener("click", (event) => {
                if (!window.confirm(element.dataset.confirm)) event.preventDefault();
            });
        });

        document.querySelectorAll("[data-busy-text]").forEach((button) => {
            const form = button.form;
            if (!form) return;
            form.addEventListener("submit", () => {
                button.disabled = true;
                button.dataset.originalText = button.innerHTML;
                button.textContent = button.dataset.busyText;
            });
        });

        document.querySelectorAll("[data-status-url]").forEach((container) => {
            const url = container.dataset.statusUrl;
            const output = container.querySelector("[data-upload-status]");
            if (!url || !output) return;
            const refresh = () => fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
                .then((response) => response.ok ? response.json() : null)
                .then((payload) => {
                    if (!payload || !payload.status) return;
                    output.textContent = payload.status_label || payload.status;
                    if (["validated", "approved", "rejected", "failed"].includes(payload.status)) clearInterval(timer);
                })
                .catch(() => undefined);
            const timer = setInterval(refresh, 5000);
            refresh();
        });
    });
})();
