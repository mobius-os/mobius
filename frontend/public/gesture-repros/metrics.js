(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(function () {});
  }

  function render() {
    var el = document.querySelector(".probe");
    if (!el) return;
    var mode = "browser";
    if (matchMedia("(display-mode: fullscreen)").matches) mode = "fullscreen";
    else if (matchMedia("(display-mode: standalone)").matches) mode = "standalone";
    else if (matchMedia("(display-mode: minimal-ui)").matches) mode = "minimal-ui";
    var vv = window.visualViewport;
    var safeBottom = getComputedStyle(document.documentElement).getPropertyValue("--safe-bottom");
    el.insertAdjacentHTML(
      "beforeend",
      "<br>display-mode: " + mode +
      "<br>innerHeight: " + window.innerHeight +
      "<br>visualViewport.height: " + (vv ? Math.round(vv.height) : "n/a") +
      "<br>safe-area-inset-bottom CSS var: " + (safeBottom || "see padding")
    );
  }

  document.documentElement.style.setProperty("--safe-bottom", "env(safe-area-inset-bottom, 0px)");
  addEventListener("load", render, { once: true });
})();
