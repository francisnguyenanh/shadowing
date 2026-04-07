// ── App data ──────────────────────────────────────────────────────────────────
const appDataEl = document.getElementById('app-data');
const appData = JSON.parse(appDataEl.textContent);
const VIDEO_ID = appData.videoId;
const DB_ID = appData.dbId;
const SEGMENTS = appData.segments; // [{id, start_time, end_time, text, ...}]

// ── State ──────────────────────────────────────────────────────────────────────
let player = null;
let pollInterval = null;
let currentSpeed = 1;
let loopEnabled = false;
let loopStart = null;
let loopEnd = null;
let activeIndex = -1;
let textVisible = true;
let translationVisible = true;
let shadowingMode = false;
let shadowingRevealTimers = {};
let timeOffset = 0; // seconds to add to segment timestamps for sync
let autoScrollEnabled = true;
let loopSegmentId = null;   // null = off; DB id of segment to loop individually
let loopRangeActive = false;      // multi-segment range loop running
let loopRangeSelecting = false;   // waiting for start/end segment selection
let loopRangeStartIdx = null;     // SEGMENTS[] index
let loopRangeEndIdx = null;       // SEGMENTS[] index
let autoPauseEnabled = false;
let showBookmarkedOnly = false;
let _lastAutoPausedIndex = -1;

// ── Chunk loop state ──────────────────────────────────────────────────────────
let _chunkLoopId    = null;   // chunk id currently looping (null = off)
let _chunkLoopStart = null;   // seconds
let _chunkLoopEnd   = null;   // seconds

// ── Practice count & daily goal ───────────────────────────────────────────────
let _loopPlayCount = {};       // {segDbId: number of loop completions this session}
let _lastLoopedSegId = null;   // track when we reset to avoid double-count
let _sessionStartTime = null;  // Date when playback began (for time tracking)
let _sessionActive = false;
let _goalMinutes = 15;
let _goalTodaySeconds = 0;

// ── YouTube IFrame API ────────────────────────────────────────────────────────

// Load API script dynamically following Google's official pattern
const tag = document.createElement('script');
tag.src = 'https://www.youtube.com/iframe_api';
const firstScriptTag = document.getElementsByTagName('script')[0];
firstScriptTag.parentNode.insertBefore(tag, firstScriptTag);

// Called automatically by YT API when ready
function onYouTubeIframeAPIReady() {
  player = new YT.Player('youtube-player', {
    videoId: VIDEO_ID,
    playerVars: {
      rel: 0,
      modestbranding: 1,
      enablejsapi: 1,
      origin: window.location.origin,
    },
    events: {
      onReady: onPlayerReady,
      onStateChange: onPlayerStateChange,
    },
  });
}

function onPlayerReady(event) {
  startPolling();
  // Restore playback speed from localStorage
  const savedSpeed = parseFloat(localStorage.getItem('shadowing_speed') || '1');
  if ([0.5, 0.75, 1, 1.25, 1.5].includes(savedSpeed)) setSpeed(savedSpeed);
  // Seek to #t=<seconds> if present in URL hash
  const hashMatch = window.location.hash.match(/[#&]t=(\d+)/);
  if (hashMatch) {
    const seekTo = parseInt(hashMatch[1], 10);
    player.seekTo(seekTo, true);
    player.playVideo();
    // Scroll transcript to that position
    const seg = SEGMENTS.find(s => s.start_time >= seekTo - 2);
    if (seg) {
      const el = document.getElementById(`seg-${seg.id}`);
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }
}

function onPlayerStateChange(event) {
  const btn = document.getElementById('play-pause-btn');
  if (!btn) return;
  if (event.data === YT.PlayerState.PLAYING) {
    btn.textContent = '⏸ Pause';
    // Start session timer
    if (!_sessionActive) {
      _sessionActive = true;
      _sessionStartTime = Date.now();
    }
  } else {
    btn.textContent = '▶ Play';
    // Flush session time
    if (_sessionActive) {
      _sessionActive = false;
      const elapsed = Math.round((Date.now() - _sessionStartTime) / 1000);
      if (elapsed >= 5) _logPracticeSession(elapsed);
      _sessionStartTime = null;
    }
  }
}

// ── Polling (250ms) ───────────────────────────────────────────────────────────

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(onPoll, 250);
}

function onPoll() {
  if (!player || typeof player.getCurrentTime !== 'function') return;

  let currentTime;
  try {
    currentTime = player.getCurrentTime();
  } catch (e) {
    return;
  }

  // Loop A-B check (against real time, before offset)
  if (loopEnabled && loopStart !== null && loopEnd !== null) {
    if (currentTime >= loopEnd) {
      player.seekTo(loopStart, true);
      return;
    }
  }

  // Chunk loop check
  if (_chunkLoopId !== null && _chunkLoopStart !== null && _chunkLoopEnd !== null) {
    if (currentTime >= _chunkLoopEnd) {
      player.seekTo(_chunkLoopStart, true);
      player.playVideo();
      return;
    }
  }

  // Apply offset: compare shifted player time against original segment timestamps
  // timeOffset > 0 means segments are earlier than video → delay matching
  // timeOffset < 0 means segments are later than video → advance matching
  const adjustedTime = currentTime - timeOffset;

  // Find active segment
  let newActiveIndex = -1;
  for (let i = 0; i < SEGMENTS.length; i++) {
    const seg = SEGMENTS[i];
    if (adjustedTime >= seg.start_time && adjustedTime < seg.end_time) {
      newActiveIndex = i;
      break;
    }
  }

  if (newActiveIndex !== activeIndex) {
    // Remove active from old
    if (activeIndex >= 0) {
      const oldEl = document.querySelector(`.segment-item[data-index="${activeIndex}"]`);
      if (oldEl) {
        oldEl.classList.remove('active', 'revealed');
        // Clear shadowing reveal timer for old segment
        if (shadowingRevealTimers[activeIndex]) {
          clearTimeout(shadowingRevealTimers[activeIndex]);
          delete shadowingRevealTimers[activeIndex];
        }
      }
    }

    activeIndex = newActiveIndex;

    if (activeIndex >= 0) {
      const newEl = document.querySelector(`.segment-item[data-index="${activeIndex}"]`);
      if (newEl) {
        newEl.classList.add('active');

        // Shadowing mode: reveal text after 1.5s
        if (shadowingMode) {
          shadowingRevealTimers[activeIndex] = setTimeout(() => {
            newEl.classList.add('revealed');
          }, 1500);
        }

        // Auto-scroll to keep active segment visible
        scrollToSegment(newEl);
      }
    }
  }

  // ── Loop range (multi-segment) ────────────────────────────────────────────
  if (loopRangeActive && loopRangeStartIdx !== null && loopRangeEndIdx !== null) {
    const endSeg = SEGMENTS[loopRangeEndIdx];
    const startSeg = SEGMENTS[loopRangeStartIdx];
    if (endSeg && startSeg && adjustedTime >= endSeg.end_time) {
      player.seekTo(startSeg.start_time + timeOffset, true);
      player.playVideo();
      return;
    }
  }

  // ── Loop single segment ───────────────────────────────────────────────────
  if (loopSegmentId !== null) {
    const loopSeg = SEGMENTS.find(s => s.id === loopSegmentId);
    if (loopSeg && adjustedTime >= loopSeg.end_time) {
      // Count one completion per crossing (debounce: 1 per segment duration minimum)
      const segDur = Math.max(1, loopSeg.end_time - loopSeg.start_time);
      const now = Date.now();
      const lastCounted = _loopPlayCount[loopSegmentId + '_ts'] || 0;
      if (now - lastCounted > segDur * 700) { // 70% of duration in ms
        _loopPlayCount[loopSegmentId + '_ts'] = now;
        _incrementPracticeCount(loopSegmentId);
      }
      player.seekTo(loopSeg.start_time + timeOffset, true);
      player.playVideo();
      return;
    }
  }

  // ── Auto-pause at segment end ─────────────────────────────────────────────
  if (autoPauseEnabled && activeIndex < 0 && adjustedTime > 0) {
    // We just left a segment (crossed end boundary)
    const prevIdx = SEGMENTS.findIndex(s => adjustedTime > s.end_time &&
      adjustedTime < s.end_time + 0.5);
    if (prevIdx >= 0 && prevIdx !== _lastAutoPausedIndex) {
      _lastAutoPausedIndex = prevIdx;
      player.pauseVideo();
    }
  } else if (activeIndex >= 0) {
    _lastAutoPausedIndex = -1;
  }
}

function scrollToSegment(el) {
  if (!autoScrollEnabled) return;
  const panel = document.getElementById('lyrics-panel');
  if (!panel || !el) return;

  const panelRect = panel.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();

  const isVisible = elRect.top >= panelRect.top && elRect.bottom <= panelRect.bottom;
  if (!isVisible) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

// ── Playback controls ─────────────────────────────────────────────────────────

// ── A: Loop single segment ────────────────────────────────────────────────────

function toggleLoopSegment(segDbId) {
  // If in range selection mode, use click to define range start/end
  if (loopRangeSelecting) {
    const idx = SEGMENTS.findIndex(s => s.id === segDbId);
    if (idx < 0) return;
    if (loopRangeStartIdx === null) {
      loopRangeStartIdx = idx;
      _updateRangeUI();
    } else {
      const a = Math.min(loopRangeStartIdx, idx);
      const b = Math.max(loopRangeStartIdx, idx);
      if (a === b) { clearRangeLoop(); return; }
      loopRangeStartIdx = a;
      loopRangeEndIdx = b;
      loopRangeSelecting = false;
      loopRangeActive = true;
      loopSegmentId = null;
      _updateRangeUI();
    }
    return;
  }
  // If range loop is active, any 🔂 click clears it
  if (loopRangeActive) {
    clearRangeLoop();
    return;
  }
  // Normal single-segment loop
  loopSegmentId = (loopSegmentId === segDbId) ? null : segDbId;
  _updateSingleLoopUI();
}

function _updateSingleLoopUI() {
  document.querySelectorAll('.seg-loop-btn').forEach(btn => {
    const id = parseInt(btn.dataset.segId);
    btn.classList.toggle('active-loop', id === loopSegmentId);
    btn.title = (id === loopSegmentId) ? 'Đang lặp segment này – Click để tắt' : 'Lặp segment này';
    btn.textContent = (id === loopSegmentId) ? '🔂 ON' : '🔂';
  });
}

// ── B2: Range loop (multi-segment) ───────────────────────────────────────────

function toggleRangeLoopMode() {
  if (loopRangeActive || loopRangeSelecting) {
    clearRangeLoop();
  } else {
    loopRangeSelecting = true;
    loopRangeStartIdx = null;
    loopRangeEndIdx = null;
    loopSegmentId = null;
    _updateSingleLoopUI();
    _updateRangeUI();
  }
}

function clearRangeLoop() {
  loopRangeActive = false;
  loopRangeSelecting = false;
  loopRangeStartIdx = null;
  loopRangeEndIdx = null;
  _updateRangeUI();
}

function _updateRangeUI() {
  const btn = document.getElementById('range-loop-btn');
  if (!btn) return;
  if (loopRangeActive) {
    btn.textContent = `🔂 ${loopRangeStartIdx + 1}–${loopRangeEndIdx + 1} ✕`;
    btn.className = 'text-xs bg-purple-800 hover:bg-purple-700 border border-purple-600 text-purple-200 px-2 py-1.5 rounded-lg transition';
  } else if (loopRangeSelecting) {
    btn.textContent = loopRangeStartIdx === null ? '🔂 Chọn đầu…' : '🔂 Chọn cuối…';
    btn.className = 'text-xs bg-yellow-800 hover:bg-yellow-700 border border-yellow-600 text-yellow-200 px-2 py-1.5 rounded-lg transition';
  } else {
    btn.textContent = '🔂 Range';
    btn.className = 'text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-2 py-1.5 rounded-lg transition';
  }
  document.querySelectorAll('.seg-loop-btn').forEach(loopBtn => {
    const segId = parseInt(loopBtn.dataset.segId);
    const idx = SEGMENTS.findIndex(s => s.id === segId);
    if (loopRangeActive && idx >= loopRangeStartIdx && idx <= loopRangeEndIdx) {
      loopBtn.classList.add('active-loop');
      if (idx === loopRangeStartIdx) { loopBtn.textContent = '🔂 S'; loopBtn.title = 'Điểm đầu range – click để xóa'; }
      else if (idx === loopRangeEndIdx) { loopBtn.textContent = '🔂 E'; loopBtn.title = 'Điểm cuối range – click để xóa'; }
      else { loopBtn.textContent = '🔂'; loopBtn.title = 'Trong range – click để xóa'; }
    } else if (loopRangeSelecting && idx === loopRangeStartIdx) {
      loopBtn.classList.add('active-loop');
      loopBtn.textContent = '🔂 S';
      loopBtn.title = 'Điểm đầu đã chọn – click đoạn khác để set điểm cuối';
    } else {
      loopBtn.classList.remove('active-loop');
      loopBtn.textContent = '🔂';
      loopBtn.title = loopRangeSelecting
        ? (loopRangeStartIdx === null ? 'Click để set làm điểm đầu range' : 'Click để set làm điểm cuối range')
        : 'Lặp segment này';
    }
  });
}

// ── C: Auto-pause mode ────────────────────────────────────────────────────────

function toggleAutoPause() {
  autoPauseEnabled = !autoPauseEnabled;
  _lastAutoPausedIndex = -1;
  const btn = document.getElementById('auto-pause-btn');
  if (!btn) return;
  if (autoPauseEnabled) {
    btn.textContent = '⏸ Auto-pause ON';
    btn.classList.replace('bg-gray-800', 'bg-teal-800');
    btn.classList.replace('border-gray-700', 'border-teal-600');
    btn.classList.replace('text-gray-300', 'text-teal-200');
  } else {
    btn.textContent = '⏸ Auto-pause';
    btn.classList.replace('bg-teal-800', 'bg-gray-800');
    btn.classList.replace('border-teal-600', 'border-gray-700');
    btn.classList.replace('text-teal-200', 'text-gray-300');
  }
}

// ── D: Jump to next/prev segment ──────────────────────────────────────────────

function jumpToSegment(delta) {
  if (!player) return;
  const idx = activeIndex >= 0 ? activeIndex : 0;
  const target = idx + delta;
  if (target < 0 || target >= SEGMENTS.length) return;
  const seg = SEGMENTS[target];
  player.seekTo(seg.start_time + timeOffset, true);
  player.playVideo();
}

// ── E: Bookmark ───────────────────────────────────────────────────────────────

function toggleBookmark(event, btnEl) {
  event.stopPropagation();
  const row = btnEl.closest('.segment-item');
  const segDbId = parseInt(row.dataset.segId);
  const idx = SEGMENTS.findIndex(s => s.id === segDbId);
  if (idx < 0) return;
  const newVal = btnEl.dataset.bookmarked !== 'true' ? 1 : 0;
  btnEl.dataset.bookmarked = newVal ? 'true' : 'false';
  btnEl.textContent = newVal ? '★' : '☆';
  btnEl.classList.toggle('text-yellow-400', !!newVal);
  btnEl.classList.toggle('text-gray-600', !newVal);
  SEGMENTS[idx].bookmarked = newVal;
  fetch(`/api/segment/${segDbId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bookmarked: newVal })
  }).catch(() => {});
  // Respect filter checkbox
  const filterCb = document.getElementById('filter-bookmarked');
  if (filterCb && filterCb.checked) filterByBookmark(true);
}

function filterByBookmark(onlyBookmarked) {
  document.querySelectorAll('.segment-item').forEach(row => {
    const btn = row.querySelector('.btn-bookmark');
    const isBookmarked = btn && btn.dataset.bookmarked === 'true';
    row.style.display = (!onlyBookmarked || isBookmarked) ? '' : 'none';
  });
}

function togglePlayPause() {
  if (!player) return;
  try {
    const state = player.getPlayerState();
    if (state === YT.PlayerState.PLAYING) {
      player.pauseVideo();
    } else {
      player.playVideo();
    }
  } catch (e) {}
}

function seekRelative(seconds) {
  if (!player) return;
  try {
    const current = player.getCurrentTime();
    player.seekTo(Math.max(0, current + seconds), true);
  } catch (e) {}
}

function seekToSegment(startTime) {
  if (!player) return;
  try {
    player.seekTo(startTime, true);
    player.playVideo();
  } catch (e) {}
}

// ── Speed control ─────────────────────────────────────────────────────────────

function setSpeed(speed) {
  currentSpeed = speed;
  localStorage.setItem('shadowing_speed', speed);
  if (player) {
    try {
      player.setPlaybackRate(speed);
    } catch (e) {}
  }
  document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.classList.toggle('active', parseFloat(btn.dataset.speed) === speed);
  });
}

// ── Loop A-B ──────────────────────────────────────────────────────────────────

function toggleLoop() {
  loopEnabled = !loopEnabled;
  const btn = document.getElementById('loop-btn');
  const controls = document.getElementById('loop-ab-controls');

  if (loopEnabled) {
    btn.textContent = '🔁 Loop ON';
    btn.classList.replace('bg-gray-800', 'bg-indigo-700');
    btn.classList.replace('border-gray-700', 'border-indigo-600');
    controls.style.display = 'flex';
  } else {
    btn.textContent = '🔁 Loop OFF';
    btn.classList.replace('bg-indigo-700', 'bg-gray-800');
    btn.classList.replace('border-indigo-600', 'border-gray-700');
    controls.style.display = 'none';
  }
}

function setLoopA() {
  if (!player) return;
  try {
    loopStart = player.getCurrentTime();
    const btn = document.getElementById('set-a-btn');
    btn.textContent = `A: ${formatTime(loopStart)}`;
  } catch (e) {}
}

function setLoopB() {
  if (!player) return;
  try {
    loopEnd = player.getCurrentTime();
    const btn = document.getElementById('set-b-btn');
    btn.textContent = `B: ${formatTime(loopEnd)}`;
  } catch (e) {}
}

function clearLoop() {
  loopStart = null;
  loopEnd = null;
  document.getElementById('set-a-btn').textContent = 'Set A';
  document.getElementById('set-b-btn').textContent = 'Set B';
}

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ── Chunk loop ─────────────────────────────────────────────────────────────────

function toggleChunkLoop(chunkId, start, end, btn) {
  if (_chunkLoopId === chunkId) {
    // Already looping this chunk → stop
    stopChunkLoop();
    return;
  }
  // Stop any existing chunk loop first
  _clearChunkLoopUI();

  _chunkLoopId    = chunkId;
  _chunkLoopStart = start;
  _chunkLoopEnd   = end;

  // Start playing from chunk start
  if (player) {
    try { player.seekTo(start, true); player.playVideo(); } catch(e) {}
  }

  // Update button UI
  btn.classList.add('bg-indigo-700', 'border-indigo-500', 'text-white');
  btn.classList.remove('bg-gray-800', 'border-gray-700', 'text-gray-300');

  // Show stop button & status
  const stopBtn = document.getElementById('chunk-loop-stop-btn');
  if (stopBtn) stopBtn.classList.remove('hidden');
  const status = document.getElementById('chunk-loop-status');
  if (status) {
    status.textContent = `🔁 Đang loop ${btn.textContent.trim().split('\n')[0].trim()} · ${formatTime(start)} – ${formatTime(end)}`;
    status.classList.remove('hidden');
  }
}

function stopChunkLoop() {
  _clearChunkLoopUI();
  _chunkLoopId    = null;
  _chunkLoopStart = null;
  _chunkLoopEnd   = null;
}

function _clearChunkLoopUI() {
  // Reset all chunk loop buttons
  document.querySelectorAll('.chunk-loop-btn').forEach(b => {
    b.classList.remove('bg-indigo-700', 'border-indigo-500', 'text-white');
    b.classList.add('bg-gray-800', 'border-gray-700', 'text-gray-300');
  });
  const stopBtn = document.getElementById('chunk-loop-stop-btn');
  if (stopBtn) stopBtn.classList.add('hidden');
  const status = document.getElementById('chunk-loop-status');
  if (status) status.classList.add('hidden');
}

// ── Text / Translation / Furigana toggles ─────────────────────────────────────

function toggleText() {
  textVisible = !textVisible;
  const panel = document.getElementById('lyrics-panel');
  const btn = document.getElementById('toggle-text-btn');
  if (textVisible) {
    panel.classList.remove('text-hidden');
    if (btn) { btn.classList.remove('btn-active'); btn.style.color = ''; }
  } else {
    panel.classList.add('text-hidden');
    if (btn) { btn.classList.add('btn-active'); btn.style.color = '#818cf8'; }
  }
}

function toggleAutoScroll() {
  autoScrollEnabled = !autoScrollEnabled;
  const btn = document.getElementById('auto-scroll-btn');
  if (!btn) return;
  btn.style.color = autoScrollEnabled ? '' : '#fbbf24';
  btn.title = autoScrollEnabled ? 'Tự động cuộn' : 'Auto-scroll đang TẮT';
}

function toggleTranslation() {
  translationVisible = !translationVisible;
  const panel = document.getElementById('lyrics-panel');
  const btn = document.getElementById('toggle-translation-btn');
  if (translationVisible) {
    panel.classList.remove('translation-hidden');
    if (btn) { btn.classList.remove('btn-active'); btn.style.color = ''; }
  } else {
    panel.classList.add('translation-hidden');
    if (btn) { btn.classList.add('btn-active'); btn.style.color = '#818cf8'; }
  }
}

// ── Shadowing mode ────────────────────────────────────────────────────────────

function toggleShadowingMode() {
  const checkbox = document.getElementById('shadowing-mode');
  shadowingMode = checkbox.checked;
  const panel = document.getElementById('lyrics-panel');

  if (shadowingMode) {
    panel.classList.add('shadowing-mode');
    // Remove all revealed classes when toggling on
    document.querySelectorAll('.segment-item.revealed').forEach(el => {
      el.classList.remove('revealed');
    });
    // Clear any pending timers
    Object.values(shadowingRevealTimers).forEach(t => clearTimeout(t));
    shadowingRevealTimers = {};
  } else {
    panel.classList.remove('shadowing-mode');
    // Clear all timers
    Object.values(shadowingRevealTimers).forEach(t => clearTimeout(t));
    shadowingRevealTimers = {};
  }
}

// ── Timeline offset ───────────────────────────────────────────────────────────

function _updateOffsetDisplay() {
  const el = document.getElementById('offset-display');
  if (!el) return;
  const sign = timeOffset > 0 ? '+' : '';
  el.textContent = sign + timeOffset.toFixed(1) + 's';
  el.style.color = timeOffset === 0 ? '#facc15' : (timeOffset > 0 ? '#f87171' : '#34d399');
}

function adjustOffset(delta) {
  timeOffset = Math.round((timeOffset + delta) * 10) / 10; // avoid float drift
  _updateOffsetDisplay();
}

function resetOffset() {
  timeOffset = 0;
  _updateOffsetDisplay();
}

async function saveOffset() {
  if (timeOffset === 0) {
    alert('Offset đang là 0, không cần lưu.');
    return;
  }
  const confirmMsg = `Lưu offset ${timeOffset > 0 ? '+' : ''}${timeOffset}s vào DB?\n\nThao tác này sẽ cộng ${timeOffset}s vào TẤT CẢ timestamps trong cơ sở dữ liệu (vĩnh viễn).`;
  if (!confirm(confirmMsg)) return;

  try {
    const res = await fetch(`/api/timeline_offset/${DB_ID}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ offset: timeOffset })
    });
    const data = await res.json();
    if (data.success) {
      // Apply offset to in-memory SEGMENTS so sync stays correct
      SEGMENTS.forEach(s => {
        s.start_time = Math.max(0, s.start_time + timeOffset);
        s.end_time = Math.max(0, s.end_time + timeOffset);
      });
      // Reset offset display
      timeOffset = 0;
      _updateOffsetDisplay();
      // Re-render timestamp badges in lyrics panel
      document.querySelectorAll('.segment-item').forEach((el, i) => {
        const tsBadge = el.querySelector('.ts-badge');
        if (tsBadge && SEGMENTS[i]) {
          const t = SEGMENTS[i].start_time;
          const m = Math.floor(t / 60);
          const s = Math.floor(t % 60);
          tsBadge.textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        }
      });
      alert(`✓ Đã lưu! ${data.rows_updated} segments được cập nhật.`);
    } else {
      alert('Lỗi lưu offset: ' + (data.error || 'Không xác định'));
    }
  } catch (e) {
    alert('Lỗi kết nối: ' + e.message);
  }
}

// ── Transcript Reader Modal ───────────────────────────────────────────────────

function openReader() {
  renderReader();
  document.getElementById('reader-modal').classList.remove('hidden');
}

function closeReader() {
  document.getElementById('reader-modal').classList.add('hidden');
}

function closeReaderOutside(e) {
  if (e.target === document.getElementById('reader-modal')) closeReader();
}

function renderReader() {
  const showTranslation = document.getElementById('reader-show-translation').checked;
  const body = document.getElementById('reader-body');
  if (!body) return;

  if (SEGMENTS.length === 0) {
    body.innerHTML = '<p class="text-gray-500 text-sm text-center py-8">Chưa có transcript.</p>';
    return;
  }

  let html = '';
  for (const seg of SEGMENTS) {
    html += `<div class="border-b border-gray-800 pb-3 cursor-pointer hover:bg-gray-800 rounded px-2 -mx-2 transition"
                  onclick="closeReader(); seekToSegment(${seg.start_time})">`;

    // Timestamp
    const m = Math.floor(seg.start_time / 60);
    const s = Math.floor(seg.start_time % 60);
    html += `<span class="text-xs font-mono text-gray-600">${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}</span> `;

    // Main text
    html += `<span class="text-white font-medium">${_escHtml(seg.text)}</span>`;

    // Translation
    if (showTranslation && seg.translation) {
      html += `<div class="text-sm text-emerald-400 italic mt-0.5">${_escHtml(seg.translation)}</div>`;
    }

    html += '</div>';
  }
  body.innerHTML = html;
}

function _escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function copyReaderText() {
  const showTranslation = document.getElementById('reader-show-translation').checked;
  let text = '';
  for (const seg of SEGMENTS) {
    const m = Math.floor(seg.start_time / 60);
    const s = Math.floor(seg.start_time % 60);
    text += `[${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}] ${seg.text}\n`;
    if (showTranslation && seg.translation) text += `       ${seg.translation}\n`;
    text += '\n';
  }
  const btn = document.getElementById('reader-copy-btn');
  navigator.clipboard.writeText(text.trim()).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓ Đã copy!';
    setTimeout(() => { btn.textContent = orig; }, 2000);
  }).catch(() => {});
}

// ── Individual segment timestamp editing ──────────────────────────────────────

// Track hovered segment for keyboard shortcuts
let hoveredSegEl = null;

(function initHoverTracking() {
  document.addEventListener('mouseover', (e) => {
    const seg = e.target.closest('.segment-item');
    if (seg) hoveredSegEl = seg;
  });
  document.addEventListener('mouseleave', (e) => {
    if (e.target.id === 'lyrics-panel') hoveredSegEl = null;
  }, true);
})();

// Keyboard shortcuts: [ = set start (cascade), ] = set end of hovered segment
// D: Space, ArrowRight, ArrowLeft, r, l, p
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.contentEditable === 'true') return;

  if (e.key === '[' && hoveredSegEl) {
    e.preventDefault();
    quickCapture(hoveredSegEl, 'start');
  } else if (e.key === ']' && hoveredSegEl) {
    e.preventDefault();
    quickCapture(hoveredSegEl, 'end');
  } else if (e.key === ' ') {
    e.preventDefault();
    togglePlayPause();
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    jumpToSegment(1);
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    jumpToSegment(-1);
  } else if (e.key === 'r') {
    e.preventDefault();
    seekRelative(-3);
  } else if (e.key === 'l') {
    e.preventDefault();
    if (activeIndex >= 0) toggleLoopSegment(SEGMENTS[activeIndex].id);
  } else if (e.key === 'p') {
    e.preventDefault();
    toggleAutoPause();
  }
});

// Apply a list of {id, start_time, end_time} updates to the DOM and SEGMENTS[]
function applySegmentUpdates(updated) {
  for (const u of updated) {
    const segEl = document.querySelector(`.segment-item[data-seg-id="${u.id}"]`);
    if (!segEl) continue;
    segEl.dataset.start = u.start_time;
    segEl.dataset.end   = u.end_time;
    const badge = segEl.querySelector('.ts-badge');
    if (badge) {
      const m = Math.floor(u.start_time / 60);
      const s = Math.floor(u.start_time % 60);
      badge.textContent = String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
    }
    // Update onclick seek target on play zone
    const playZone = segEl.querySelector('.seg-play-zone');
    if (playZone) playZone.setAttribute('onclick', `segmentDivClick(event, ${u.start_time})`);
    // Sync in-memory SEGMENTS[]
    const idx = parseInt(segEl.dataset.index);
    if (!isNaN(idx) && SEGMENTS[idx]) {
      SEGMENTS[idx].start_time = u.start_time;
      SEGMENTS[idx].end_time   = u.end_time;
    }
  }
}

// One-click capture: get current player time → save immediately
// Setting start cascades: prev.end = newStart, subsequent segments shift by delta
async function quickCapture(segEl, field) {
  if (!player) return;
  let t;
  try { t = Math.round(player.getCurrentTime() * 10) / 10; } catch (e) { return; }

  const segDbId = parseInt(segEl.dataset.segId);
  if (!segDbId) return;

  const btn = segEl.querySelector(field === 'start' ? '.ts-quick-start' : '.ts-quick-end');
  const oldStart = parseFloat(segEl.dataset.start);

  // For end-only: must be > start
  if (field === 'end' && t <= oldStart) return;

  const payload = field === 'start'
    ? { start_time: t, cascade: true }
    : { end_time: t };

  if (btn) { btn.classList.add('ts-saving'); btn.textContent = '…'; }

  try {
    const res = await fetch(`/api/segment/${segDbId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      applySegmentUpdates(data.updated || []);
      if (btn) {
        btn.classList.remove('ts-saving');
        btn.classList.add('ts-saved');
        btn.textContent = '✓';
        setTimeout(() => {
          btn.classList.remove('ts-saved');
          btn.textContent = '⌚ Start';
        }, 1200);
      }
    } else {
      if (btn) { btn.classList.remove('ts-saving'); btn.textContent = '⌚ Start'; }
    }
  } catch (_) {
    if (btn) { btn.classList.remove('ts-saving'); btn.textContent = '⌚ Start'; }
  }
}

// ── handleSegEditKey — Enter saves, Escape reverts ────────────────────────────

function handleSegEditKey(event, el) {
  if (event.key === 'Enter') {
    event.preventDefault();
    el.blur();
  } else if (event.key === 'Escape') {
    event.preventDefault();
    if (el.dataset.original !== undefined) el.textContent = el.dataset.original;
    el.blur();
  }
}

// Store original text on focus so Escape can revert
(function initSegEditableFocus() {
  document.addEventListener('focusin', (e) => {
    if (e.target.classList.contains('seg-editable')) {
      e.target.dataset.original = e.target.textContent;
    }
  });
})();

function segmentDivClick(event, startTime) {
  if (event.target.tagName === 'INPUT' || event.target.tagName === 'BUTTON' || event.target.tagName === 'KBD') return;
  if (event.target.contentEditable === 'true') return;
  seekToSegment(startTime);
}

function usePlayerTime(input) {
  if (!player) return;
  try {
    const t = Math.round(player.getCurrentTime() * 10) / 10;
    input.value = t;
  } catch (e) {}
}

function onTsFocusOut(event, input) {
  const segEl = input.closest('.segment-item');
  const relatedTarget = event.relatedTarget;
  if (relatedTarget && segEl.contains(relatedTarget)) return;
  saveSegmentTime(segEl);
}

async function saveSegmentTime(segEl) {
  const segDbId = parseInt(segEl.dataset.segId);
  if (!segDbId) return;
  const startInput = segEl.querySelector('.ts-start');
  const endInput   = segEl.querySelector('.ts-end');
  if (!startInput || !endInput) return;

  const newStart = parseFloat(startInput.value);
  const newEnd   = parseFloat(endInput.value);
  const oldStart = parseFloat(segEl.dataset.start);
  const oldEnd   = parseFloat(segEl.dataset.end);

  if (isNaN(newStart) || isNaN(newEnd) || newStart < 0 || newEnd <= newStart) {
    startInput.value = oldStart;
    endInput.value   = oldEnd;
    return;
  }
  if (Math.abs(newStart - oldStart) < 0.001 && Math.abs(newEnd - oldEnd) < 0.001) return;

  const startChanged = Math.abs(newStart - oldStart) >= 0.001;

  try {
    const res = await fetch(`/api/segment/${segDbId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_time: newStart, end_time: newEnd, cascade: startChanged })
    });
    const data = await res.json();
    if (data.success) {
      applySegmentUpdates(data.updated || []);
      // Green flash on current segment's inputs
      [startInput, endInput].forEach(inp => {
        inp.style.borderColor = '#22c55e';
        setTimeout(() => { inp.style.borderColor = ''; }, 1200);
      });
    } else {
      startInput.value = oldStart;
      endInput.value   = oldEnd;
    }
  } catch (e) {
    startInput.value = oldStart;
    endInput.value   = oldEnd;
  }
}

// Save editable content (text / furigana / translation) on focus-out
async function saveSegmentContent(el, field, segEl) {
  if (!segEl) return;
  const segDbId = parseInt(segEl.dataset.segId);
  if (!segDbId) return;

  const newVal = el.innerText.trim();
  const idx = parseInt(segEl.dataset.index);
  if (isNaN(idx) || !SEGMENTS[idx]) return;

  // Normalize: map DB column names (text, furigana, translation match segment keys)
  const oldVal = (SEGMENTS[idx][field] || '').trim();
  if (newVal === oldVal) return;

  try {
    const res = await fetch(`/api/segment/${segDbId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: newVal })
    });
    const data = await res.json();
    if (data.success) {
      SEGMENTS[idx][field] = newVal;
      el.style.outline = '1px solid #22c55e';
      setTimeout(() => { el.style.outline = ''; }, 1000);
    } else {
      el.innerText = oldVal; // revert on error
    }
  } catch (_) {
    el.innerText = oldVal;
  }
}

// ── Drag to resize panels ─────────────────────────────────────────────────────

(function initDragDivider() {
  const layout = document.getElementById('main-layout');
  const leftPanel = document.getElementById('left-panel');
  const rightPanel = document.getElementById('right-panel');
  const divider = document.getElementById('drag-divider');
  if (!layout || !leftPanel || !rightPanel || !divider) return;

  // Only active on desktop (lg = 1024px+)
  function isDesktop() { return window.innerWidth >= 1024; }

  // Position divider over the border between panels
  function positionDivider() {
    if (!isDesktop()) { divider.style.display = 'none'; return; }
    divider.style.display = 'block';
    const lRect = leftPanel.getBoundingClientRect();
    const layoutRect = layout.getBoundingClientRect();
    divider.style.left = (lRect.right - layoutRect.left - 3) + 'px';
    divider.style.top = '0';
    divider.style.height = layout.offsetHeight + 'px';
    divider.style.position = 'absolute';
    layout.style.position = 'relative';
  }

  positionDivider();

  // Set initial left-panel width so 16:9 video fills the full panel height
  let userHasResized = false;
  function setInitialWidth() {
    if (!isDesktop()) return;
    const layoutW = layout.getBoundingClientRect().width;
    const layoutH = layout.getBoundingClientRect().height;
    const controlsEl = leftPanel.querySelector('.flex-shrink-0');
    const controlsH = controlsEl ? controlsEl.offsetHeight : 0;
    const availableVideoH = layoutH - controlsH;
    const idealW = Math.round(availableVideoH * 16 / 9);
    const safeW = Math.max(300, Math.min(layoutW - 200, idealW));
    const leftPct = (safeW / layoutW * 100).toFixed(2);
    const rightPct = (100 - parseFloat(leftPct)).toFixed(2);
    leftPanel.style.width = leftPct + '%';
    leftPanel.style.flex = 'none';
    rightPanel.style.width = rightPct + '%';
    rightPanel.style.flex = 'none';
  }
  requestAnimationFrame(() => { setInitialWidth(); positionDivider(); });

  window.addEventListener('resize', () => {
    positionDivider();
    if (!userHasResized) setInitialWidth();
  });

  let dragging = false;
  let startX = 0;
  let startLeftW = 0;

  divider.addEventListener('mousedown', (e) => {
    if (!isDesktop()) return;
    userHasResized = true;
    dragging = true;
    startX = e.clientX;
    startLeftW = leftPanel.getBoundingClientRect().width;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const layoutW = layout.getBoundingClientRect().width;
    const delta = e.clientX - startX;
    let newLeftW = startLeftW + delta;
    // Clamp: min 200px each side
    newLeftW = Math.max(200, Math.min(layoutW - 200, newLeftW));
    const leftPct = (newLeftW / layoutW * 100).toFixed(2);
    const rightPct = (100 - parseFloat(leftPct)).toFixed(2);
    leftPanel.style.width = leftPct + '%';
    leftPanel.style.flex = 'none';
    rightPanel.style.width = rightPct + '%';
    rightPanel.style.flex = 'none';
    positionDivider();
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });

  // Touch support
  divider.addEventListener('touchstart', (e) => {
    if (!isDesktop()) return;
    userHasResized = true;
    dragging = true;
    startX = e.touches[0].clientX;
    startLeftW = leftPanel.getBoundingClientRect().width;
    e.preventDefault();
  }, { passive: false });

  document.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    const layoutW = layout.getBoundingClientRect().width;
    const delta = e.touches[0].clientX - startX;
    let newLeftW = startLeftW + delta;
    newLeftW = Math.max(200, Math.min(layoutW - 200, newLeftW));
    const leftPct = (newLeftW / layoutW * 100).toFixed(2);
    const rightPct = (100 - parseFloat(leftPct)).toFixed(2);
    leftPanel.style.width = leftPct + '%';
    leftPanel.style.flex = 'none';
    rightPanel.style.width = rightPct + '%';
    rightPanel.style.flex = 'none';
    positionDivider();
  }, { passive: false });

  document.addEventListener('touchend', () => { dragging = false; });
})();

// ── Dictation Mode ────────────────────────────────────────────────────────────

let dictationMode = false;
let dictationSegIndex = -1;  // current segment index for dictation
let dictationChecked = false;

function toggleDictationMode() {
  const checkbox = document.getElementById('dictation-mode');
  dictationMode = checkbox.checked;
  const panel = document.getElementById('dictation-panel');
  const lyricsPanel = document.getElementById('lyrics-panel');

  if (dictationMode) {
    panel.classList.remove('hidden');
    // Hide text in lyrics so user can't peek
    lyricsPanel.classList.add('dictation-active');
    // Auto-pause after each segment
    if (!autoPauseEnabled) toggleAutoPause();
    // Start from current active segment or first
    dictationSegIndex = activeIndex >= 0 ? activeIndex : 0;
    loadDictationSegment();
  } else {
    panel.classList.add('hidden');
    lyricsPanel.classList.remove('dictation-active');
    clearDictationResult();
  }
}

function loadDictationSegment() {
  if (dictationSegIndex < 0 || dictationSegIndex >= SEGMENTS.length) return;
  const seg = SEGMENTS[dictationSegIndex];
  const m = Math.floor(seg.start_time / 60);
  const s = Math.floor(seg.start_time % 60);
  const timeEl = document.getElementById('dictation-seg-time');
  if (timeEl) timeEl.textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');

  // Clear previous
  const input = document.getElementById('dictation-input');
  if (input) { input.value = ''; input.focus(); }
  clearDictationResult();
  dictationChecked = false;

  // Seek and play the segment
  if (player) {
    player.seekTo(seg.start_time + timeOffset, true);
    player.playVideo();
  }
}

function clearDictationResult() {
  const result = document.getElementById('dictation-result');
  const score = document.getElementById('dictation-score');
  if (result) result.classList.add('hidden');
  if (score) score.classList.add('hidden');
}

function replayDictationSegment() {
  if (dictationSegIndex < 0 || dictationSegIndex >= SEGMENTS.length) return;
  const seg = SEGMENTS[dictationSegIndex];
  if (player) {
    player.seekTo(seg.start_time + timeOffset, true);
    player.playVideo();
  }
}

function checkDictation() {
  if (dictationSegIndex < 0 || dictationSegIndex >= SEGMENTS.length) return;
  const seg = SEGMENTS[dictationSegIndex];
  const input = document.getElementById('dictation-input');
  const userText = (input ? input.value : '').trim();
  if (!userText) return;

  const originalText = (seg.text || '').trim();
  const result = compareDictation(userText, originalText);

  // Show score
  const scoreEl = document.getElementById('dictation-score');
  if (scoreEl) {
    scoreEl.classList.remove('hidden');
    const pct = result.accuracy;
    if (pct >= 90) {
      scoreEl.textContent = `🎉 ${pct}% — Tuyệt vời!`;
      scoreEl.className = 'ml-auto text-sm font-bold text-green-400';
    } else if (pct >= 70) {
      scoreEl.textContent = `👍 ${pct}% — Khá tốt!`;
      scoreEl.className = 'ml-auto text-sm font-bold text-yellow-400';
    } else if (pct >= 50) {
      scoreEl.textContent = `📝 ${pct}% — Cần luyện thêm`;
      scoreEl.className = 'ml-auto text-sm font-bold text-orange-400';
    } else {
      scoreEl.textContent = `💪 ${pct}% — Cố gắng hơn!`;
      scoreEl.className = 'ml-auto text-sm font-bold text-red-400';
    }
  }

  // Show diff
  const resultEl = document.getElementById('dictation-result');
  const diffEl = document.getElementById('dictation-diff');
  const origEl = document.getElementById('dictation-original');
  const transEl = document.getElementById('dictation-translation');
  if (resultEl) resultEl.classList.remove('hidden');
  if (diffEl) diffEl.innerHTML = result.diffHtml;
  if (origEl) origEl.textContent = '✓ Đáp án: ' + originalText;
  if (transEl) transEl.textContent = seg.translation ? '🌐 ' + seg.translation : '';

  dictationChecked = true;
}

function showDictationAnswer() {
  if (dictationSegIndex < 0 || dictationSegIndex >= SEGMENTS.length) return;
  const seg = SEGMENTS[dictationSegIndex];
  const resultEl = document.getElementById('dictation-result');
  const diffEl = document.getElementById('dictation-diff');
  const origEl = document.getElementById('dictation-original');
  const transEl = document.getElementById('dictation-translation');
  const scoreEl = document.getElementById('dictation-score');

  if (resultEl) resultEl.classList.remove('hidden');
  if (diffEl) diffEl.innerHTML = '<span class="text-gray-500 italic">Bạn đã xem đáp án</span>';
  if (origEl) origEl.textContent = '✓ Đáp án: ' + (seg.text || '');
  if (transEl) transEl.textContent = seg.translation ? '🌐 ' + seg.translation : '';
  if (scoreEl) { scoreEl.textContent = '👁 Xem đáp án'; scoreEl.className = 'ml-auto text-sm font-bold text-gray-400'; scoreEl.classList.remove('hidden'); }
  dictationChecked = true;
}

function nextDictationSegment() {
  // Mark current segment as done (reveal text)
  if (dictationSegIndex >= 0 && dictationSegIndex < SEGMENTS.length) {
    const curEl = document.querySelector(`.segment-item[data-index="${dictationSegIndex}"]`);
    if (curEl) curEl.classList.add('dictation-done');
  }
  dictationSegIndex++;
  if (dictationSegIndex >= SEGMENTS.length) {
    dictationSegIndex = SEGMENTS.length - 1;
    alert('Đã hết segments! Bạn đã luyện xong toàn bộ.');
    return;
  }
  loadDictationSegment();
}

function handleDictationKey(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    if (dictationChecked) {
      nextDictationSegment();
    } else {
      checkDictation();
    }
  }
}

/**
 * Compare user input vs original text. Returns {accuracy: 0-100, diffHtml: string}.
 * Uses word-level comparison (works for both Japanese and English).
 */
function compareDictation(userText, originalText) {
  // Normalize: lowercase, trim extra spaces
  const normalize = (s) => s.toLowerCase().replace(/\s+/g, ' ').trim();
  const userNorm = normalize(userText);
  const origNorm = normalize(originalText);

  if (userNorm === origNorm) {
    return { accuracy: 100, diffHtml: '<span class="text-green-400">' + _escHtml(userText) + '</span>' };
  }

  // Tokenize: split into characters for CJK, words for latin
  const tokenize = (s) => {
    // If mostly CJK, split per character
    const cjkCount = (s.match(/[\u3000-\u9fff\uff00-\uffef]/g) || []).length;
    if (cjkCount > s.length * 0.3) {
      return s.replace(/\s+/g, '').split('');
    }
    return s.split(/\s+/);
  };

  const userTokens = tokenize(userNorm);
  const origTokens = tokenize(origNorm);

  // LCS (Longest Common Subsequence) for accuracy
  const m = userTokens.length;
  const n = origTokens.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (userTokens[i - 1] === origTokens[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }
  const lcsLen = dp[m][n];
  const accuracy = n > 0 ? Math.round((lcsLen / n) * 100) : 0;

  // Backtrack LCS to build diff
  const matchSet = new Set();
  let i = m, j = n;
  while (i > 0 && j > 0) {
    if (userTokens[i - 1] === origTokens[j - 1]) {
      matchSet.add(i - 1);
      i--; j--;
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      i--;
    } else {
      j--;
    }
  }

  // Build colored HTML from user tokens
  let diffHtml = '';
  for (let k = 0; k < userTokens.length; k++) {
    const token = _escHtml(userTokens[k]);
    if (matchSet.has(k)) {
      diffHtml += '<span class="text-green-400">' + token + '</span>';
    } else {
      diffHtml += '<span class="text-red-400 line-through">' + token + '</span>';
    }
  }

  return { accuracy, diffHtml };
}

// ── Practice count ────────────────────────────────────────────────────────────

async function _incrementPracticeCount(segDbId) {
  try {
    const res = await fetch(`/api/segment/${segDbId}/practice`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      const idx = SEGMENTS.findIndex(s => s.id === segDbId);
      if (idx >= 0) SEGMENTS[idx].practice_count = data.practice_count;
      const badge = document.querySelector(`.seg-practice-badge[data-seg-id="${segDbId}"]`);
      if (badge) {
        const pc = data.practice_count;
        badge.dataset.count = pc;
        badge.title = `Đã luyện ${pc} lần`;
        if (pc >= 20)      badge.textContent = '🌺🌿🌿';
        else if (pc >= 15) badge.textContent = '🌸🌿🌿';
        else if (pc >= 10) badge.textContent = '🌿🌿';
        else if (pc >= 5)  badge.textContent = '🌿';
        else               badge.textContent = '';
      }
    }
  } catch (_) {}
}

// ── Daily goal ────────────────────────────────────────────────────────────────

async function _logPracticeSession(seconds) {
  if (seconds < 5) return;
  try {
    const res = await fetch('/api/daily_goal/log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds })
    });
    const data = await res.json();
    if (data.success) {
      _goalTodaySeconds = data.today_seconds;
      _updateGoalDisplay();
    }
  } catch (_) {}
}

function _updateGoalDisplay() {
  const goalSecs = _goalMinutes * 60;
  const pct = goalSecs > 0 ? Math.min(100, Math.round((_goalTodaySeconds / goalSecs) * 100)) : 0;
  const todayMin = Math.floor(_goalTodaySeconds / 60);
  const todaySec = _goalTodaySeconds % 60;

  const bar = document.getElementById('goal-progress-bar');
  if (bar) {
    bar.style.width = pct + '%';
    bar.style.background = pct >= 100 ? '#22c55e' : '';
  }
  const label = document.getElementById('goal-label');
  if (label) label.textContent = `${todayMin}/${_goalMinutes}p`;

  const panelBar = document.getElementById('goal-progress-bar-panel');
  if (panelBar) {
    panelBar.style.width = pct + '%';
    panelBar.style.background = pct >= 100 ? '#22c55e' : '';
  }
  const todayDisplay = document.getElementById('goal-today-display');
  if (todayDisplay) todayDisplay.textContent = `${todayMin}:${String(todaySec).padStart(2, '0')}`;
  const targetDisplay = document.getElementById('goal-target-display');
  if (targetDisplay) targetDisplay.textContent = _goalMinutes;
  const statusText = document.getElementById('goal-status-text');
  if (statusText) {
    statusText.textContent = pct >= 100
      ? '🎉 Đã hoàn thành mục tiêu hôm nay!'
      : `Còn ${_goalMinutes - todayMin} phút nữa để đạt mục tiêu`;
  }
}

function toggleGoalPanel() {
  const panel = document.getElementById('goal-panel');
  if (panel) panel.classList.toggle('hidden');
}

async function saveGoal() {
  const input = document.getElementById('goal-input');
  const minutes = parseInt(input ? input.value : '');
  if (!minutes || minutes < 1 || minutes > 480) { alert('Nhập từ 1–480 phút'); return; }
  try {
    const res = await fetch('/api/daily_goal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minutes })
    });
    const data = await res.json();
    if (data.success) {
      _goalMinutes = data.goal_minutes;
      _updateGoalDisplay();
      const panel = document.getElementById('goal-panel');
      if (panel) panel.classList.add('hidden');
    }
  } catch (_) {}
}

async function initGoal() {
  try {
    const res = await fetch('/api/daily_goal');
    const data = await res.json();
    if (!data.error) {
      _goalMinutes = data.goal_minutes;
      _goalTodaySeconds = data.today_seconds;
      _updateGoalDisplay();
      const input = document.getElementById('goal-input');
      if (input) input.placeholder = _goalMinutes;
    }
  } catch (_) {}
}

window.addEventListener('beforeunload', () => {
  if (_sessionActive && _sessionStartTime) {
    const elapsed = Math.round((Date.now() - _sessionStartTime) / 1000);
    if (elapsed >= 5) {
      navigator.sendBeacon('/api/daily_goal/log',
        new Blob([JSON.stringify({ seconds: elapsed })], { type: 'application/json' }));
    }
  }
});

initGoal();


