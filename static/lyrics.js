import { $, api, toast, error, guarded, appendText } from "./ui-common.js";
// Lyric candidate search, review, timing selection and dialog lifecycle.
export function createLyricPicker({ refresh, cancelGuestSong, videoPreview }) {
  let selectingSongId = null;
  let selectionRequest = 0;
  let lyricControllers = [];
  let lyricKeys = new Set();
  let lyricHits = 0;
  let lyricPending = 0;
  let gpuAlignment = true;
  let selectedCandidate = null;
  let currentVideo = null;

  function previewLyrics(candidate) {
    $("guest-lyric-preview-info").textContent = [
      candidate.title,
      candidate.artist,
      candidate.source
    ]
      .filter(Boolean)
      .join(" / ");
    $("guest-lyric-preview-text").textContent = candidate.unfiltered_text || candidate.text;
    $("guest-lyric-preview-dialog").showModal();
  }
  $("guest-lyric-preview-close").onclick = () => $("guest-lyric-preview-dialog").close();

  function abortLyricRequests() {
    lyricControllers.forEach((c) => c.abort());
    lyricControllers = [];
  }
  async function openLyricPicker(songId, title, query, video = null) {
    const health = await api("/api/health");
    gpuAlignment = health.gpu_alignment !== false;
    $("guest-lyrics-upload").hidden = !gpuAlignment;
    document.querySelector('label[for="guest-lyrics-upload"]').hidden = !gpuAlignment;
    $("netease-review-ai-card").hidden = !gpuAlignment;
    if (!health.auto_prepare) {
      toast("后台自动制歌已关闭，可播放已有视频或请管理员手动上传", true);
    } else if (health.autoprep_ready === false) {
      toast("后台制歌暂不可用，服务正在尝试恢复；已有视频仍可播放", true);
    }
    selectingSongId = songId;
    selectedCandidate = null;
    $("guest-lyrics-select").disabled = true;
    currentVideo = video?.bvid ? video : null;
    $("guest-lyrics-preview").hidden = !currentVideo;
    $("guest-lyrics-song").textContent = title;
    // Server supplies the same extracted name used for the backend search.
    // Show it immediately so the guest can correct an incorrect video title.
    $("guest-lyrics-query").value = (query || title).slice(0, 60);
    if (!$("guest-lyrics-dialog").open) $("guest-lyrics-dialog").showModal();
    await searchGuestLyrics();
  }
  let suspendingLyricPicker = false;
  function openNeteaseReview(candidate, useLyrics) {
    const dialog = $("netease-review-dialog");
    if (dialog.open) return;
    $("netease-review-title").textContent =
      candidate.source === "网易云音乐" ? "网易云歌词" : "歌词校正";
    const original = candidate.unfiltered_text || candidate.text;
    const lines = original.split("\n");
    // Existing cleanup only removes leading production credits.
    const defaultLines = candidate.text.split("\n");
    const removed = Math.max(0, lines.length - defaultLines.length);
    const chosen = new Set(lines.map((_, i) => i).filter((i) => i >= removed));
    const rows = $("netease-review-lines");
    rows.replaceChildren();
    const count = $("netease-review-count");
    const update = () => {
      count.textContent = `✓ 已保留 ${chosen.size} / ${lines.length} 行`;
      $("netease-review-select-all").disabled = chosen.size === lines.length;
      for (const button of rows.children) {
        const kept = chosen.has(Number(button.dataset.index));
        button.classList.toggle("kept", kept);
        button.setAttribute("aria-pressed", String(kept));
        button.querySelector(".review-check").textContent = kept ? "✓" : "✕";
      }
    };
    lines.forEach((line, i) => {
      const row = appendText(rows, "button", "", "netease-review-line");
      appendText(row, "span", "", "review-check").setAttribute("aria-hidden", "true");
      appendText(row, "span", line, "review-line-text");
      row.type = "button";
      row.dataset.index = String(i);
      row.onclick = () => {
        if (chosen.has(i)) chosen.delete(i);
        else chosen.add(i);
        update();
      };
    });
    $("netease-review-select-all").onclick = () => {
      lines.forEach((_, i) => chosen.add(i));
      update();
    };
    update();
    const providerAvailable = Boolean(candidate.timed_lrc && candidate.timing_ticket);
    $("netease-review-provider").hidden = !providerAvailable;
    $("netease-review-ai-card").hidden = !gpuAlignment;
    showNeteaseSection("netease-line-review");
    dialog
      .querySelector(".alignment-cards")
      .classList.toggle("single-mode", !providerAvailable || !gpuAlignment);
    $("netease-review-offset").value = "0";
    document.querySelector(".offset-details").open = false;
    const options = () => ({
      selection_text: original,
      selection_ticket: candidate.unfiltered_ticket || candidate.ticket,
      selected_lines: [...chosen].sort((a, b) => a - b)
    });
    const submit = (button, timed) => {
      if (chosen.size < 2) {
        toast("至少保留两行歌词", true);
        return;
      }
      const extra = options();
      if (timed) {
        const offset = $("netease-review-offset").value;
        const seconds = Number(offset);
        if (!offset || !Number.isFinite(seconds)) {
          toast("请输入有效的时间偏移", true);
          return;
        }
        Object.assign(extra, {
          timing_mode: "provider",
          timed_lrc: candidate.timed_lrc,
          source_lrc_raw: candidate.source_lrc_raw,
          timing_ticket: candidate.timing_ticket,
          offset_ms: Math.round(seconds * 1000)
        });
      }
      useLyrics(candidate.text, candidate.ticket, button, extra);
    };
    $("netease-review-align").onclick = () => submit($("netease-review-align"), false);
    $("netease-review-timed").onclick = () => submit($("netease-review-timed"), true);
    $("netease-review-back").onclick = () => dialog.close();
    const picker = $("guest-lyrics-dialog");
    if (picker.open) {
      suspendingLyricPicker = true;
      picker.close();
    }
    dialog.showModal();
  }
  // One section always owns the available height; the other is a compact header.
  function showNeteaseSection(active) {
    for (const [sectionId, contentId] of [
      ["netease-line-review", "netease-review-content"],
      ["netease-alignment-choice", "netease-alignment-cards"]
    ]) {
      const expanded = sectionId === active;
      $(sectionId).classList.toggle("is-open", expanded);
      $(contentId).hidden = !expanded;
      $(sectionId)
        .querySelector(".netease-accordion-toggle")
        .setAttribute("aria-expanded", String(expanded));
    }
  }
  for (const id of ["netease-line-review", "netease-alignment-choice"]) {
    $(id).querySelector(".netease-accordion-toggle").onclick = () => showNeteaseSection(id);
  }
  $("netease-review-dialog").addEventListener("close", () => {
    // Back button and Esc restore the same candidate list, without re-searching.
    if (selectingSongId !== null && !$("guest-lyrics-dialog").open) {
      $("guest-lyrics-dialog").showModal();
    }
  });
  function useLyrics(candidate, text, ticket, button, extra = {}) {
    return guarded(async () => {
      const songId = selectingSongId;
      if (songId === null) return;
      await api("/api/songs/" + songId + "/lyrics/select", "POST", {
        text,
        ticket,
        source: candidate.source,
        ...extra
      });
      selectionRequest++;
      abortLyricRequests();
      selectingSongId = null;
      selectedCandidate = null;
      if ($("netease-review-dialog").open) $("netease-review-dialog").close();
      if ($("guest-lyrics-dialog").open) $("guest-lyrics-dialog").close();
      toast("✓ 歌词已选择");
      await refresh();
    }, button);
  }
  $("guest-lyrics-select").onclick = () => {
    const candidate = selectedCandidate;
    if (!candidate) return;
    const apply = (text, ticket, button, extra) =>
      useLyrics(candidate, text, ticket, button, extra);
    if (
      candidate.source === "网易云音乐" ||
      (candidate.timed_lrc && candidate.timing_ticket) ||
      (candidate.unfiltered_text && candidate.unfiltered_ticket)
    ) {
      try {
        openNeteaseReview(candidate, apply);
      } catch (e) {
        error(e);
      }
    } else apply(candidate.text, candidate.ticket, $("guest-lyrics-select"));
  };
  function addLyricCandidates(candidates, songId, sequence) {
    if (sequence !== selectionRequest || selectingSongId !== songId) return;
    const panel = $("guest-lyrics-results");
    candidates.forEach((candidate) => {
      if (!gpuAlignment && !(candidate.timed_lrc && candidate.timing_ticket)) return;
      // Source and timing must stay distinct even if the lyric texts match.
      const key = [candidate.source, candidate.url, candidate.text].join("\u0000");
      if (lyricKeys.has(key)) return;
      lyricKeys.add(key);
      if (lyricHits === 0) panel.replaceChildren();
      lyricHits++;
      const item = appendText(panel, "div", "", "lyric-choice");
      const card = appendText(item, "button", "", "lyric-option choice-card");
      card.type = "button";
      card.setAttribute("aria-pressed", "false");
      const heading = appendText(card, "span", "", "lyric-option-header");
      appendText(heading, "strong", candidate.title);
      appendText(heading, "span", candidate.source);
      if (candidate.artist) appendText(card, "span", candidate.artist, "lyric-artist");
      appendText(card, "span", candidate.text, "lyric-excerpt");
      const preview = appendText(item, "button", "预览", "btn subtle lyric-preview-btn");
      preview.type = "button";
      preview.setAttribute("aria-label", `预览${candidate.title}的完整歌词`);
      preview.onclick = () => previewLyrics(candidate);
      card.onclick = () => {
        selectedCandidate = candidate;
        for (const option of panel.querySelectorAll(".lyric-option"))
          option.setAttribute("aria-pressed", String(option === card));
        $("guest-lyrics-select").disabled = false;
      };
    });
    updateLyricSearchStatus();
  }
  function updateLyricSearchStatus() {
    $("guest-lyrics-status").textContent = lyricHits
      ? lyricPending
        ? "正在查找更多歌词……"
        : ""
      : lyricPending
        ? "正在查找歌词……"
        : gpuAlignment
          ? "没有找到候选，可以更换关键词或上传 LRC"
          : "当前只能使用带时间轴的歌词，可换关键词或选择不加字幕";
  }
  async function fetchLyricPhase(phase, songId, query, sequence) {
    const controller = new AbortController();
    lyricControllers.push(controller);
    lyricPending++;
    const deadline = setTimeout(() => controller.abort(), 20000);
    try {
      const url =
        "/api/songs/" +
        songId +
        "/lyrics/candidates?phase=" +
        phase +
        "&query=" +
        encodeURIComponent(query);
      const data = await api(url, "GET", null, controller.signal, 20000);
      if (sequence !== selectionRequest || selectingSongId !== songId) return;
      addLyricCandidates(data.candidates, songId, sequence);
    } catch (e) {
      // A failed or slow provider contributes no cards. Other providers continue.
      if (e.name !== "AbortError") console.info("Lyrics provider", phase, e.message);
    } finally {
      clearTimeout(deadline);
      lyricControllers = lyricControllers.filter((c) => c !== controller);
      if (sequence === selectionRequest && selectingSongId === songId) {
        lyricPending--;
        if (!lyricHits && !lyricPending) {
          $("guest-lyrics-results").replaceChildren();
          appendText(
            $("guest-lyrics-results"),
            "div",
            gpuAlignment
              ? "暂无歌词候选，可修改关键词或上传 LRC"
              : "未找到带逐句时间轴的歌词，可修改关键词或直接使用原片",
            "empty"
          );
        }
        updateLyricSearchStatus();
      }
    }
  }
  async function searchGuestLyrics() {
    const songId = selectingSongId;
    if (songId === null) return;
    const sequence = ++selectionRequest;
    abortLyricRequests();
    lyricKeys = new Set();
    selectedCandidate = null;
    $("guest-lyrics-select").disabled = true;
    lyricHits = 0;
    lyricPending = 0;
    const query = $("guest-lyrics-query").value.trim();
    const panel = $("guest-lyrics-results");
    panel.replaceChildren();
    if (!query) {
      appendText(panel, "div", "请先输入歌名", "empty");
      return;
    }
    appendText(panel, "div", "正在查询歌词来源……", "empty");
    $("guest-lyrics-more").disabled = false;
    const sources = ["vcpedia", "wiki", "netease", "lrclib"];
    // Each request adds results immediately on arrival, regardless of the others.
    await Promise.allSettled(sources.map((src) => fetchLyricPhase(src, songId, query, sequence)));
  }
  async function searchMoreLyrics() {
    const songId = selectingSongId;
    if (songId === null) return;
    const sequence = selectionRequest;
    const query = $("guest-lyrics-query").value.trim();
    if (!query) return;
    const button = $("guest-lyrics-more");
    button.disabled = true;
    await fetchLyricPhase("more", songId, query, sequence);
    if (sequence === selectionRequest && selectingSongId === songId) button.disabled = false;
  }
  $("guest-lyrics-dialog").addEventListener("close", () => {
    if ($("guest-lyric-preview-dialog").open) $("guest-lyric-preview-dialog").close();
    if (suspendingLyricPicker) {
      suspendingLyricPicker = false;
      return;
    }
    selectingSongId = null;
    selectedCandidate = null;
    currentVideo = null;
    selectionRequest++;
    abortLyricRequests();
  });
  $("guest-lyrics-preview").onclick = () => {
    if (currentVideo) videoPreview.watch(currentVideo);
  };
  $("guest-lyrics-skip").onclick = () =>
    guarded(async () => {
      const songId = selectingSongId;
      if (songId === null) return;
      await api(`/api/songs/${songId}/lyrics/skip`, "POST");
      $("guest-lyrics-dialog").close();
      toast("✓ 已选择使用原片，不制作字幕");
      await refresh();
    }, $("guest-lyrics-skip"));
  $("guest-lyrics-cancel").onclick = () =>
    guarded(() => cancelGuestSong(selectingSongId), $("guest-lyrics-cancel"));
  $("guest-lyrics-upload").onchange = () =>
    guarded(async () => {
      const input = $("guest-lyrics-upload"),
        file = input.files[0],
        songId = selectingSongId;
      if (!file || songId === null) return;
      try {
        if (!file.name.toLowerCase().endsWith(".lrc") || file.size > 1024 * 1024)
          throw new Error("请选择不超过 1 MiB 的 .lrc 文件");
        const data = new FormData();
        data.append("file", file);
        await api(`/api/songs/${songId}/lyrics/upload`, "POST", data);
        $("guest-lyrics-dialog").close();
        toast("✓ 已上传歌词，将重新对齐时间轴");
        await refresh();
      } finally {
        input.value = "";
      }
    });
  $("guest-lyrics-search").onclick = () =>
    guarded(() => searchGuestLyrics(), $("guest-lyrics-search"));
  $("guest-lyrics-more").onclick = () => guarded(() => searchMoreLyrics(), $("guest-lyrics-more"));
  $("guest-lyrics-query").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      $("guest-lyrics-search").click();
      e.preventDefault();
    }
  });
  return { openLyricPicker, selectedSongId: () => selectingSongId };
}
