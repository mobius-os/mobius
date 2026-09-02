(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(function () {});
  }

  var insetProbe = document.createElement("div");
  insetProbe.setAttribute("aria-hidden", "true");
  insetProbe.style.cssText = [
    "position:fixed", "inset:0 auto auto 0", "width:0", "height:0",
    "visibility:hidden", "pointer-events:none",
    "padding-top:env(safe-area-inset-top,0px)",
    "padding-right:env(safe-area-inset-right,0px)",
    "padding-bottom:env(safe-area-inset-bottom,0px)",
    "padding-left:env(safe-area-inset-left,0px)"
  ].join(";");
  document.body.appendChild(insetProbe);

  function rounded(value) {
    return typeof value === "number" ? Math.round(value * 100) / 100 : "n/a";
  }

  function rectLine(name, element) {
    if (!element) return name + ": n/a";
    var rect = element.getBoundingClientRect();
    return name + ": " + rounded(rect.width) + "×" + rounded(rect.height) +
      " @ " + rounded(rect.left) + "," + rounded(rect.top);
  }

  function displayMode() {
    if (matchMedia("(display-mode: fullscreen)").matches) return "fullscreen";
    if (matchMedia("(display-mode: standalone)").matches) return "standalone";
    if (matchMedia("(display-mode: minimal-ui)").matches) return "minimal-ui";
    return "browser";
  }

  function render(reason) {
    var host = document.querySelector(".probe");
    if (!host) return;
    var output = host.querySelector("[data-viewport-metrics]");
    if (!output) {
      output = document.createElement("pre");
      output.setAttribute("data-viewport-metrics", "");
      output.style.cssText = "margin:12px 0 0;white-space:pre-wrap;overflow-wrap:anywhere";
      host.appendChild(output);
    }
    var vv = window.visualViewport;
    var insetStyle = getComputedStyle(insetProbe);
    output.textContent = [
      "updated: " + reason,
      "display-mode: " + displayMode(),
      "visibility: " + document.visibilityState,
      "inner: " + window.innerWidth + "×" + window.innerHeight,
      "client: " + document.documentElement.clientWidth + "×" + document.documentElement.clientHeight,
      "screen: " + screen.width + "×" + screen.height + " @" + window.devicePixelRatio + "x",
      "orientation: " + (screen.orientation && screen.orientation.type || "n/a"),
      "visualViewport: " + (vv
        ? rounded(vv.width) + "×" + rounded(vv.height) + " @ " +
          rounded(vv.offsetLeft) + "," + rounded(vv.offsetTop) + " scale " + rounded(vv.scale)
        : "n/a"),
      rectLine("html rect", document.documentElement),
      rectLine("body rect", document.body),
      rectLine("root rect", document.getElementById("root")),
      "safe insets T/R/B/L: " + [
        insetStyle.paddingTop, insetStyle.paddingRight,
        insetStyle.paddingBottom, insetStyle.paddingLeft
      ].join(" / ")
    ].join("\n");
  }

  function visibleRender() {
    if (document.visibilityState === "visible") render("visibilitychange");
  }

  addEventListener("load", function () { render("load"); }, { once: true });
  addEventListener("resize", function () { render("window.resize"); });
  addEventListener("orientationchange", function () { render("orientationchange"); });
  addEventListener("pageshow", function () { render("pageshow"); });
  document.addEventListener("visibilitychange", visibleRender);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", function () { render("visualViewport.resize"); });
    window.visualViewport.addEventListener("scroll", function () { render("visualViewport.scroll"); });
  }
})();
