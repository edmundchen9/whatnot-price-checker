// Throwaway spike — NOT the real extension.
//
// Goal: find out whether Whatnot's live-stream <video> element can be read
// via a <canvas> (i.e. isn't CORS-tainted). If it can, a real extension can
// grab pixel-perfect video frames directly instead of doing OS-level screen
// capture (no permission prompts, no DPI/scaling guessing, no compositing
// unrelated windows). If the canvas comes back tainted, this whole approach
// is dead and we'd need chrome.tabCapture instead.
//
// Usage: load this folder as an unpacked extension, open a Whatnot live
// stream, click somewhere on the page body (not the chat textbox), and
// press "T". Check the console and the badge in the bottom-right corner.

(function () {
  const BADGE_ID = "__wpc_capture_spike_badge";

  function pickMainVideo() {
    const videos = Array.from(document.querySelectorAll("video"));
    if (videos.length === 0) return null;
    // Heuristic: the live stream player is almost certainly the largest
    // visible <video> on the page (sidebar/thumbnail videos are smaller).
    let best = null;
    let bestArea = 0;
    for (const v of videos) {
      const rect = v.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area > bestArea && rect.width > 0 && rect.height > 0) {
        bestArea = area;
        best = v;
      }
    }
    return best;
  }

  function showBadge(html) {
    let el = document.getElementById(BADGE_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = BADGE_ID;
      el.style.position = "fixed";
      el.style.bottom = "16px";
      el.style.right = "16px";
      el.style.zIndex = "2147483647";
      el.style.background = "#15151a";
      el.style.border = "1px solid #3ddc84";
      el.style.borderRadius = "8px";
      el.style.padding = "10px";
      el.style.color = "#fff";
      el.style.fontFamily = "Menlo, monospace";
      el.style.fontSize = "11px";
      el.style.maxWidth = "260px";
      el.style.boxShadow = "0 4px 16px rgba(0,0,0,0.5)";
      document.body.appendChild(el);
    }
    el.innerHTML = html;
  }

  function runTest() {
    const video = pickMainVideo();
    if (!video) {
      console.warn("[WPC SPIKE] No <video> element found on this page.");
      showBadge("No &lt;video&gt; element found on this page.");
      return;
    }

    console.log("[WPC SPIKE] Found video:", {
      src: video.currentSrc || video.src,
      crossOrigin: video.crossOrigin,
      videoWidth: video.videoWidth,
      videoHeight: video.videoHeight,
      readyState: video.readyState,
    });

    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || video.clientWidth;
    canvas.height = video.videoHeight || video.clientHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    let dataUrl;
    try {
      dataUrl = canvas.toDataURL("image/png");
    } catch (err) {
      console.error("[WPC SPIKE] Canvas is TAINTED — cannot read pixels:", err);
      showBadge(
        "\u274c Canvas is tainted (CORS). Direct video capture won't work " +
          "this way; would need chrome.tabCapture instead. See console."
      );
      return;
    }

    console.log("[WPC SPIKE] SUCCESS \u2014 canvas read without error.", {
      dataUrlLength: dataUrl.length,
      size: `${canvas.width}x${canvas.height}`,
    });
    showBadge(
      `\u2705 Capture OK (${canvas.width}x${canvas.height}). Snapshot:` +
        `<br><img src="${dataUrl}" style="max-width:240px;border:1px solid #3ddc84;border-radius:4px;margin-top:6px;display:block;" />`
    );
  }

  window.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() !== "t" || e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
    runTest();
  });

  console.log(
    "[WPC SPIKE] Loaded. Click the page body (not the chat box) then press 'T' to test video capture."
  );
})();
