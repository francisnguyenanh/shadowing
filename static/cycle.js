// cycle.js — JS for the per-chunk 3-day cycle detail page

const _cycleData = JSON.parse(document.getElementById('cycle-data').textContent);
const VIDEO_DB_ID = _cycleData.videoDbId;
const CHUNK_ID = _cycleData.chunkId;
let CYCLE_ID = _cycleData.cycleId;
let CYCLE_STATUS = _cycleData.cycleStatus;

// ── Cycle management ──────────────────────────────────────────────────────────

async function startCycle() {
  const btn = event?.target || document.querySelector('[onclick="startCycle()"]');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Đang xử lý…'; }
  try {
    const res = await fetch(`/api/chunk/${CHUNK_ID}/cycle/start`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      window.location.reload();
    } else {
      alert('Lỗi: ' + (data.error || 'Không xác định'));
      if (btn) { btn.disabled = false; btn.textContent = '🚀 Bắt đầu chu kỳ'; }
    }
  } catch (e) {
    alert('Lỗi kết nối: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '🚀 Bắt đầu chu kỳ'; }
  }
}

async function restartCycle() {
  if (!confirm('Xóa tiến trình cũ và bắt đầu lại từ đầu?')) return;
  await startCycle();
}

async function advanceCycle() {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = '⏳…'; }
  try {
    const res = await fetch(`/api/cycle-by-id/${CYCLE_ID}/advance`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      window.location.reload();
    } else {
      alert('Lỗi: ' + (data.error || 'Không xác định'));
      if (btn) { btn.disabled = false; btn.textContent = '→ Chuyển sang ngày tiếp'; }
    }
  } catch (e) {
    alert('Lỗi kết nối: ' + e.message);
  }
}

// ── Activity completion ───────────────────────────────────────────────────────

async function toggleActivity(activityId, btn) {
  const row = document.getElementById(`act-row-${activityId}`);
  const isNowCompleted = !btn.classList.contains('bg-emerald-600');

  // Optimistic UI update
  _setActivityUI(btn, row, isNowCompleted);

  try {
    const res = await fetch(`/api/activity/${activityId}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ completed: isNowCompleted })
    });
    const data = await res.json();
    if (!data.success) {
      // Revert
      _setActivityUI(btn, row, !isNowCompleted);
      alert('Lỗi: ' + (data.error || 'Không xác định'));
    } else {
      _refreshDayProgress();
    }
  } catch (e) {
    _setActivityUI(btn, row, !isNowCompleted);
  }
}

function _setActivityUI(btn, row, completed) {
  if (completed) {
    btn.classList.add('bg-emerald-600', 'border-emerald-600', 'text-white');
    btn.classList.remove('border-gray-600');
    btn.innerHTML = '<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>';
    row?.querySelectorAll('p.text-sm').forEach(p => p.classList.add('line-through', 'text-gray-500'));
  } else {
    btn.classList.remove('bg-emerald-600', 'border-emerald-600', 'text-white');
    btn.classList.add('border-gray-600');
    btn.innerHTML = '';
    row?.querySelectorAll('p.text-sm').forEach(p => p.classList.remove('line-through', 'text-gray-500'));
  }
}

async function _refreshDayProgress() {
  try {
    const res = await fetch(`/api/cycle/${VIDEO_DB_ID}/status`);
    const data = await res.json();
    if (!data.progress) return;
    for (const [day, prog] of Object.entries(data.progress)) {
      const d = parseInt(day);
      const bar = document.querySelector(`#day-section-${d} .h-2.bg-gray-800 div`);
      const label = document.querySelector(`#day-section-${d} .text-xs.text-gray-400 span`);
      if (bar) bar.style.width = prog.pct + '%';
      // update day card pct
      const cardBar = document.querySelectorAll('.grid.grid-cols-3 > div')[d - 1]?.querySelector('.h-2 div');
      if (cardBar) cardBar.style.width = prog.pct + '%';
    }
  } catch (e) {}
}

// ── Day accordion ─────────────────────────────────────────────────────────────

function toggleDay(dayNum) {
  const body = document.getElementById(`day-body-${dayNum}`);
  const chevron = document.getElementById(`day-chevron-${dayNum}`);
  if (body.classList.contains('hidden')) {
    body.classList.remove('hidden');
    chevron?.classList.add('rotate-180');
  } else {
    body.classList.add('hidden');
    chevron?.classList.remove('rotate-180');
  }
}

// ── Focus Expressions ─────────────────────────────────────────────────────────

let _exprChunkId = null;
let _exprList = [];

function openExprEditor(chunkId, currentExprs) {
  _exprChunkId = chunkId;
  _exprList = Array.isArray(currentExprs) ? [...currentExprs] : [];
  renderExprTags();
  document.getElementById('expr-modal').classList.remove('hidden');
  document.getElementById('expr-input').focus();
}

function closeExprModal() {
  document.getElementById('expr-modal').classList.add('hidden');
  _exprChunkId = null;
}

function renderExprTags() {
  const container = document.getElementById('expr-tags');
  container.innerHTML = _exprList.map((e, i) => `
    <span class="flex items-center gap-1 bg-yellow-900 text-yellow-300 border border-yellow-700 text-xs px-2 py-1 rounded-full">
      ${_esc(e)}
      <button onclick="removeExpr(${i})" class="hover:text-red-300 transition ml-1">×</button>
    </span>
  `).join('');
}

function removeExpr(idx) {
  _exprList.splice(idx, 1);
  renderExprTags();
}

function addExpr() {
  const input = document.getElementById('expr-input');
  const val = input.value.trim();
  if (!val) return;
  if (_exprList.length >= 15) { alert('Tối đa 15 expressions.'); return; }
  if (!_exprList.includes(val)) _exprList.push(val);
  input.value = '';
  renderExprTags();
}

async function saveExpressions() {
  if (_exprChunkId === null) return;
  try {
    const res = await fetch(`/api/chunks/${_exprChunkId}/expressions`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expressions: _exprList })
    });
    const data = await res.json();
    if (data.success) {
      // Update display
      const display = document.getElementById(`expr-display-${_exprChunkId}`);
      if (display) {
        if (_exprList.length === 0) {
          display.innerHTML = '<span class="text-xs text-gray-600 italic">Chưa có focus expressions</span>';
        } else {
          display.innerHTML = _exprList.map(e =>
            `<span class="bg-yellow-900 text-yellow-300 border border-yellow-700 text-xs px-2 py-0.5 rounded-full">${_esc(e)}</span>`
          ).join('');
        }
      }
      closeExprModal();
    } else {
      alert('Lỗi: ' + (data.error || 'Không xác định'));
    }
  } catch (e) {
    alert('Lỗi kết nối: ' + e.message);
  }
}

// ── Comprehension slider ──────────────────────────────────────────────────────

function updateComprehensionDisplay(day, val) {
  const el = document.getElementById(`comp-display-${day}`);
  if (el) el.textContent = val + '%';
}

async function saveComprehension(day, val) {
  if (!CYCLE_ID) return;
  try {
    await fetch(`/api/cycle-by-id/${CYCLE_ID}/comprehension`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ day, pct: parseInt(val) })
    });
  } catch (e) {}
}

// ── Audio Recorder ────────────────────────────────────────────────────────────

let _recVideoId = null;
let _recActivityId = null;
let _recActivityType = 'free_recall';
let _mediaRecorder = null;
let _recChunks = [];
let _recBlob = null;
let _recTimerInterval = null;
let _recSeconds = 0;

function openRecorder(videoId, activityId, activityType) {
  _recVideoId = videoId;
  _recActivityId = activityId;
  _recActivityType = activityType || 'free_recall';

  const labels = {
    'free_recall': 'Nói lại Chunk A không nhìn script (2 phút)',
    'free_recall_b': 'Nói lại Chunk B không nhìn script (2 phút)',
    'record_evaluate': 'Nói tự do 2 phút + Ghi âm tự đánh giá',
    'free_speech': 'Nói tự do 5 phút dùng chunks vừa học',
  };
  document.getElementById('recorder-label').textContent = labels[activityType] || activityType;

  // Reset UI
  document.getElementById('rec-timer').textContent = '0:00';
  document.getElementById('rec-waveform').classList.add('hidden');
  document.getElementById('rec-playback').classList.add('hidden');
  document.getElementById('rec-start-btn').disabled = false;
  document.getElementById('rec-stop-btn').disabled = true;
  document.getElementById('rec-notes').value = '';
  _recBlob = null;
  _recChunks = [];

  document.getElementById('recorder-modal').classList.remove('hidden');
}

function closeRecorderModal() {
  stopRecording();
  document.getElementById('recorder-modal').classList.add('hidden');
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _recChunks = [];
    _mediaRecorder = new MediaRecorder(stream);
    _mediaRecorder.ondataavailable = e => { if (e.data.size > 0) _recChunks.push(e.data); };
    _mediaRecorder.onstop = () => {
      _recBlob = new Blob(_recChunks, { type: 'audio/webm' });
      const url = URL.createObjectURL(_recBlob);
      const audio = document.getElementById('rec-audio');
      audio.src = url;
      document.getElementById('rec-playback').classList.remove('hidden');
      stream.getTracks().forEach(t => t.stop());
    };
    _mediaRecorder.start();

    // UI
    document.getElementById('rec-start-btn').disabled = true;
    document.getElementById('rec-stop-btn').disabled = false;
    document.getElementById('rec-waveform').classList.remove('hidden');

    // Timer
    _recSeconds = 0;
    _recTimerInterval = setInterval(() => {
      _recSeconds++;
      const m = Math.floor(_recSeconds / 60);
      const s = _recSeconds % 60;
      document.getElementById('rec-timer').textContent = `${m}:${s.toString().padStart(2, '0')}`;
    }, 1000);
  } catch (e) {
    alert('Không thể truy cập microphone: ' + e.message);
  }
}

function stopRecording() {
  if (_mediaRecorder && _mediaRecorder.state !== 'inactive') {
    _mediaRecorder.stop();
  }
  if (_recTimerInterval) { clearInterval(_recTimerInterval); _recTimerInterval = null; }
  document.getElementById('rec-start-btn').disabled = false;
  document.getElementById('rec-stop-btn').disabled = true;
  document.getElementById('rec-waveform').classList.add('hidden');
}

async function saveRecording() {
  if (!_recBlob) { alert('Chưa có bản ghi âm.'); return; }
  const notes = document.getElementById('rec-notes').value.trim();
  const btn = document.querySelector('[onclick="saveRecording()"]');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Đang lưu…'; }

  const formData = new FormData();
  formData.append('audio', _recBlob, `recording.webm`);
  formData.append('video_id', _recVideoId);
  if (_recActivityId) formData.append('activity_id', _recActivityId);
  formData.append('activity_type', _recActivityType);
  formData.append('duration', _recSeconds);
  if (notes) formData.append('notes', notes);

  try {
    const res = await fetch('/api/recording/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.success) {
      closeRecorderModal();
      // Auto-complete the activity
      if (_recActivityId) {
        const actBtn = document.querySelector(`#act-row-${_recActivityId} button[onclick^="toggleActivity"]`);
        if (actBtn && !actBtn.classList.contains('bg-emerald-600')) {
          await toggleActivity(_recActivityId, actBtn);
        }
      }
      window.location.reload();
    } else {
      alert('Lỗi lưu: ' + (data.error || 'Không xác định'));
      if (btn) { btn.disabled = false; btn.textContent = '💾 Lưu bản ghi'; }
    }
  } catch (e) {
    alert('Lỗi kết nối: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '💾 Lưu bản ghi'; }
  }
}

async function deleteRecording(recId) {
  if (!confirm('Xóa bản ghi âm này?')) return;
  try {
    const res = await fetch(`/api/recording/${recId}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      const row = document.getElementById(`rec-row-${recId}`);
      if (row) { row.style.opacity = '0'; setTimeout(() => row.remove(), 300); }
    } else {
      alert('Lỗi: ' + data.error);
    }
  } catch (e) {
    alert('Lỗi: ' + e.message);
  }
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function _esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
