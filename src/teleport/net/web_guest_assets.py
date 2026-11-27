# ruff: noqa: E501
"""Tài nguyên giao diện Web chế độ khách (Web Guest Mode) cho ZCL Teleport (v1.3.0).

Toàn bộ trang web được đóng gói trong một file HTML duy nhất (< 50 KB),
nhúng sẵn CSS và JavaScript thuần, hỗ trợ chế độ sáng/tối tự động và đa ngôn ngữ (VI/EN).
Không phụ thuộc bất kỳ thư viện hay CDN bên ngoài nào, hoạt động ngoại tuyến 100% trong mạng nội bộ.
"""

from __future__ import annotations

WEB_GUEST_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ZCL Teleport - Chuyển tệp nội bộ</title>
  <style>
    :root {
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
      --primary: #0284c7;
      --primary-hover: #0369a1;
      --success: #16a34a;
      --danger: #dc2626;
      --radius: 12px;
      --shadow: 0 4px 6px -1px rgba(0,0,0,0.06), 0 2px 4px -2px rgba(0,0,0,0.04);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f172a;
        --card-bg: #1e293b;
        --text: #f8fafc;
        --text-muted: #94a3b8;
        --border: #334155;
        --primary: #38bdf8;
        --primary-hover: #7dd3fc;
        --success: #22c55e;
        --danger: #ef4444;
        --shadow: 0 4px 6px -1px rgba(0,0,0,0.3);
      }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
    }
    .container {
      width: 100%;
      max-width: 540px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 16px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand svg {
      width: 28px;
      height: 28px;
      stroke: var(--primary);
    }
    .brand h1 {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .peer-info {
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .lang-btn {
      background: none;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 600;
    }
    .tabs {
      display: flex;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 4px;
      box-shadow: var(--shadow);
    }
    .tab-btn {
      flex: 1;
      padding: 10px;
      text-align: center;
      border: none;
      background: none;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.95rem;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: all 0.2s ease;
    }
    .tab-btn.active {
      background: var(--primary);
      color: #ffffff;
    }
    .tab-btn svg {
      width: 18px;
      height: 18px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px;
      box-shadow: var(--shadow);
    }
    .dropzone {
      border: 2px dashed var(--border);
      border-radius: var(--radius);
      padding: 28px 20px;
      text-align: center;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      transition: border-color 0.2s, background-color 0.2s;
    }
    .dropzone:active, .dropzone.dragover {
      border-color: var(--primary);
      background: rgba(2, 132, 199, 0.06);
    }
    .dropzone svg {
      width: 44px;
      height: 44px;
      stroke: var(--primary);
    }
    .btn-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: center;
    }
    .btn-select {
      background: var(--primary);
      color: #ffffff;
      border: none;
      padding: 10px 18px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
    }
    .btn-select-secondary {
      background: transparent;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 10px 16px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
    }
    .download-toolbar {
      display: flex;
      justify-content: flex-end;
      margin-bottom: 12px;
    }
    .file-list {
      margin-top: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    #downloadSection .file-list {
      margin-top: 0;
    }
    .file-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg);
    }
    .file-details {
      display: flex;
      flex-direction: column;
      gap: 4px;
      overflow: hidden;
      flex: 1;
      margin-right: 12px;
    }
    .file-name {
      font-weight: 600;
      font-size: 0.9rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .file-meta {
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    .progress-bar-bg {
      width: 100%;
      height: 6px;
      background: var(--border);
      border-radius: 3px;
      overflow: hidden;
      margin-top: 4px;
    }
    .progress-bar-fill {
      height: 100%;
      background: var(--primary);
      width: 0%;
      transition: width 0.15s linear;
    }
    .btn-action {
      background: var(--primary);
      color: #ffffff;
      border: none;
      padding: 8px 14px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 0.85rem;
      cursor: pointer;
      text-decoration: none;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn-action svg {
      width: 16px;
      height: 16px;
    }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-success { background: rgba(22, 163, 74, 0.15); color: var(--success); }
    .badge-danger { background: rgba(220, 38, 38, 0.15); color: var(--danger); }
    .empty-state {
      text-align: center;
      color: var(--text-muted);
      padding: 32px 16px;
      font-size: 0.9rem;
    }
    footer {
      text-align: center;
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: auto;
      padding: 16px 0;
    }
    .badge-scope { background: rgba(2, 132, 199, 0.14); color: var(--primary); margin-left: 6px; }
    .text-form { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
    .text-input {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg);
      color: var(--text);
      font-family: inherit;
      font-size: 0.92rem;
      resize: vertical;
    }
    .text-form-actions { display: flex; justify-content: flex-end; }
    .text-item {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--bg);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .text-item-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.78rem;
      color: var(--text-muted);
    }
    .text-body { font-size: 0.92rem; white-space: pre-wrap; word-break: break-word; }
    .text-body a { color: var(--primary); text-decoration: underline; }
    .btn-mini {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      cursor: pointer;
    }
    .banner-expired {
      border-color: var(--danger);
      color: var(--danger);
      font-weight: 600;
      text-align: center;
      font-size: 0.9rem;
    }
    .tab-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: 9px;
      background: var(--danger);
      color: #ffffff;
      font-size: 0.72rem;
      font-weight: 700;
    }
    .tab-btn.active .tab-badge {
      background: #ffffff;
      color: var(--primary);
    }
    .batch-box {
      margin-top: 14px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .batch-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.82rem;
      color: var(--text-muted);
      gap: 8px;
    }
    .file-thumb {
      width: 40px;
      height: 40px;
      border-radius: 6px;
      object-fit: cover;
      border: 1px solid var(--border);
      margin-right: 10px;
      flex-shrink: 0;
    }
    .file-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .badge-muted { background: rgba(100, 116, 139, 0.16); color: var(--text-muted); }
    .hidden { display: none !important; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="8" x2="12" y2="12"></line>
          <line x1="12" y1="12" x2="16" y2="16"></line>
        </svg>
        <div>
          <h1>ZCL Teleport <span class="badge badge-scope hidden" id="scopeBadge"></span></h1>
          <div class="peer-info" id="peerInfo">Kết nối nội bộ trực tiếp</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;">
        <button type="button" class="lang-btn" id="soundBtn" onclick="toggleSound()" aria-label="Âm thanh" style="display:inline-flex;align-items:center;justify-content:center;width:30px;height:28px;padding:0;"></button>
        <button class="lang-btn" id="langBtn" onclick="toggleLang()">EN</button>
      </div>
    </header>

    <div class="card banner-expired hidden" id="expiredBanner"></div>

    <div class="tabs">
      <button class="tab-btn active" id="tabUploadBtn" onclick="switchTab('upload')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="17 8 12 3 7 8"></polyline>
          <line x1="12" y1="3" x2="12" y2="15"></line>
        </svg>
        <span id="tabUploadText">Gửi sang máy tính</span>
      </button>
      <button class="tab-btn" id="tabDownloadBtn" onclick="switchTab('download')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="7 10 12 15 17 10"></polyline>
          <line x1="12" y1="15" x2="12" y2="3"></line>
        </svg>
        <span id="tabDownloadText">Nhận về điện thoại</span>
        <span class="tab-badge hidden" id="badgeDownload">0</span>
      </button>
      <button class="tab-btn" id="tabTextBtn" onclick="switchTab('text')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
        </svg>
        <span id="tabTextLabel">Văn bản</span>
        <span class="tab-badge hidden" id="badgeText">0</span>
      </button>
    </div>

    <!-- Tab Gửi sang máy tính -->
    <div class="card" id="uploadSection">
      <input type="file" id="filePicker" multiple style="display:none;" onchange="handleFilesSelected(this.files)">
      <input type="file" id="folderPicker" webkitdirectory directory multiple style="display:none;" onchange="handleFilesSelected(this.files)">
      <input type="file" id="cameraInput" accept="image/*" capture="environment" style="display:none;" onchange="handleFilesSelected(this.files)">
      <div class="dropzone" id="dropZone" onclick="document.getElementById('filePicker').click()">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"></path>
          <path d="M12 12v9"></path>
          <path d="m16 16-4-4-4 4"></path>
        </svg>
        <div>
          <strong id="dropzoneTitle">Chọn hoặc kéo thả tệp, thư mục</strong>
          <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;" id="dropzoneSub">Hỗ trợ chọn nhiều tệp hoặc cả thư mục cùng lúc</div>
        </div>
        <div class="btn-row">
          <button type="button" class="btn-select" id="btnSelectText" onclick="event.stopPropagation(); document.getElementById('filePicker').click()">Chọn tệp</button>
          <button type="button" class="btn-select-secondary" id="btnSelectFolderText" onclick="event.stopPropagation(); document.getElementById('folderPicker').click()">Chọn thư mục</button>
          <button type="button" class="btn-select-secondary" id="btnCameraText" onclick="event.stopPropagation(); document.getElementById('cameraInput').click()">Chụp ảnh gửi ngay</button>
        </div>
      </div>

      <div class="batch-box hidden" id="batchBox">
        <div class="batch-head">
          <span id="batchLabel"></span>
          <span id="batchEta"></span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" id="batchFill"></div>
        </div>
      </div>

      <div class="file-list" id="uploadList"></div>
    </div>

    <!-- Tab Nhận về điện thoại -->
    <div class="card hidden" id="downloadSection">
      <div class="download-toolbar hidden" id="downloadToolbar">
        <a class="btn-action" id="downloadAllBtn" href="#">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="7 10 12 15 17 10"></polyline>
            <line x1="12" y1="15" x2="12" y2="3"></line>
          </svg>
          <span id="downloadAllText">Tải tất cả (.zip)</span>
        </a>
      </div>
      <div class="file-list" id="downloadList">
        <div class="empty-state" id="downloadEmpty">Không có tệp nào đang chờ nhận từ máy tính.</div>
      </div>
    </div>

    <!-- Tab Văn bản / Liên kết -->
    <div class="card hidden" id="textSection">
      <div class="text-form" id="textForm">
        <textarea class="text-input" id="textInput" rows="3" placeholder="Nhập văn bản hoặc dán liên kết cần gửi sang máy tính..."></textarea>
        <div class="text-form-actions">
          <button type="button" class="btn-action" id="sendTextBtn" onclick="sendText()">Gửi văn bản</button>
        </div>
      </div>
      <div class="file-list" id="textList">
        <div class="empty-state" id="textEmpty">Chưa có tin nhắn văn bản nào trong phiên này.</div>
      </div>
    </div>

    <footer>
      <div>ZCL Teleport · Kết nối nội bộ trực tiếp · Không qua trung gian</div>
      <div style="margin-top: 4px;">Copyright (C) 2026 Nguyễn Hoàng Tùng</div>
    </footer>
  </div>

  <script>
    const urlParams = new URLSearchParams(window.location.search);
    const token = urlParams.get('token') || '';
    let currentLang = (navigator.language || '').toLowerCase().startsWith('vi') ? 'vi' : 'en';
    let lastFiles = [];
    let lastTexts = [];
    let seenFileIds = null;
    let seenTextIds = null;
    let unreadFiles = 0;
    let unreadTexts = 0;
    let currentScope = 'both';
    let currentOneShot = false;
    let sessionExpired = false;
    let activeTab = 'upload';
    let peerDeviceName = '';
    let soundEnabled = true;
    try { soundEnabled = localStorage.getItem('zcl_sound') !== '0'; } catch (_) {}
    let audioCtx = null;

    function unlockSfx() {
      if (!soundEnabled) return null;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!audioCtx) {
        try { audioCtx = new AC(); } catch (_) { return null; }
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume().catch(() => {});
      }
      return audioCtx;
    }

    function playSfx(kind) {
      if (!soundEnabled) return;
      const ctx = unlockSfx();
      if (!ctx || ctx.state !== 'running') return;
      const notes = kind === 'done' ? [[523, 0.08], [659, 0.12]]
        : kind === 'err' ? [[311, 0.14]]
        : [[587, 0.07]];
      let at = ctx.currentTime;
      for (const [freq, dur] of notes) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, at);
        gain.gain.setValueAtTime(0.001, at);
        gain.gain.linearRampToValueAtTime(0.08, at + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.001, at + dur);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(at);
        osc.stop(at + dur + 0.01);
        at += dur * 0.72;
      }
    }

    function updateSoundBtn() {
      const btn = document.getElementById('soundBtn');
      if (!btn) return;
      btn.title = currentLang === 'vi'
        ? (soundEnabled ? 'Tắt âm thông báo' : 'Bật âm thông báo')
        : (soundEnabled ? 'Mute sound' : 'Unmute sound');
      btn.setAttribute('aria-label', btn.title);
      btn.style.opacity = soundEnabled ? '1' : '0.55';
      btn.innerHTML = soundEnabled
        ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>'
        : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line></svg>';
    }

    function toggleSound() {
      soundEnabled = !soundEnabled;
      try { localStorage.setItem('zcl_sound', soundEnabled ? '1' : '0'); } catch (_) {}
      if (soundEnabled) unlockSfx();
      updateSoundBtn();
    }

    document.addEventListener('pointerdown', unlockSfx, { once: true });
    document.addEventListener('keydown', unlockSfx, { once: true });

    const I18N = {
      vi: {
        peer: 'Kết nối máy tính: {name}',
        tabUpload: 'Gửi sang máy tính',
        tabDownload: 'Nhận về điện thoại',
        tabText: 'Văn bản',
        dropTitle: 'Chọn hoặc kéo thả tệp, thư mục',
        dropSub: 'Hỗ trợ chọn nhiều tệp hoặc cả thư mục cùng lúc',
        btnSelect: 'Chọn tệp',
        btnSelectFolder: 'Chọn thư mục',
        btnCamera: 'Chụp ảnh gửi ngay',
        downloadAll: 'Tải tất cả (.zip)',
        emptyDownload: 'Không có tệp nào đang chờ nhận từ máy tính.',
        downloadBtn: 'Tải về',
        folderTag: 'Thư mục',
        uploading: 'Đang gửi... {percent}% ({speed})',
        completed: 'Hoàn tất',
        canceled: 'Đã hủy',
        cancelBtn: 'Hủy',
        retryBtn: 'Thử lại',
        batchProgress: 'Tổng tiến độ: {percent}% ({done} / {total})',
        etaLeft: 'Còn ~{eta}',
        failed: 'Thất bại: {err}',
        waiting: 'Đang chờ...',
        textPlaceholder: 'Nhập văn bản hoặc dán liên kết cần gửi sang máy tính...',
        sendText: 'Gửi văn bản',
        emptyText: 'Chưa có tin nhắn văn bản nào trong phiên này.',
        fromHost: 'Từ máy tính',
        fromGuest: 'Từ điện thoại',
        copy: 'Sao chép',
        copied: 'Đã chép',
        scopeReceiveOnly: 'Chỉ gửi sang PC',
        scopeSendOnly: 'Chỉ nhận từ PC',
        oneShotBadge: '1 lần',
        expiredOneShot: 'Phiên chỉ dùng 1 lượt đã hoàn tất và tự động đóng.',
      },
      en: {
        peer: 'Connected PC: {name}',
        tabUpload: 'Send to Computer',
        tabDownload: 'Receive on Phone',
        tabText: 'Text',
        dropTitle: 'Select or drop files & folders',
        dropSub: 'Supports multiple files or whole folders at once',
        btnSelect: 'Choose Files',
        btnSelectFolder: 'Choose Folder',
        btnCamera: 'Take Photo & Send',
        downloadAll: 'Download All (.zip)',
        emptyDownload: 'No files waiting to download from computer.',
        downloadBtn: 'Download',
        folderTag: 'Folder',
        uploading: 'Sending... {percent}% ({speed})',
        completed: 'Finished',
        canceled: 'Canceled',
        cancelBtn: 'Cancel',
        retryBtn: 'Retry',
        batchProgress: 'Batch progress: {percent}% ({done} / {total})',
        etaLeft: '~{eta} left',
        failed: 'Failed: {err}',
        waiting: 'Pending...',
        textPlaceholder: 'Type text or paste link to send to computer...',
        sendText: 'Send Text',
        emptyText: 'No text messages in this session yet.',
        fromHost: 'From Computer',
        fromGuest: 'From Phone',
        copy: 'Copy',
        copied: 'Copied',
        scopeReceiveOnly: 'Upload only',
        scopeSendOnly: 'Download only',
        oneShotBadge: 'One-shot',
        expiredOneShot: 'This one-shot session has finished and closed.',
      }
    };

    function toggleLang() {
      currentLang = currentLang === 'vi' ? 'en' : 'vi';
      updateTexts();
    }

    function updateTexts() {
      const t = I18N[currentLang];
      document.documentElement.lang = currentLang;
      document.getElementById('langBtn').innerText = currentLang === 'vi' ? 'EN' : 'VI';
      if (peerDeviceName) {
        document.getElementById('peerInfo').innerText = t.peer.replace('{name}', peerDeviceName);
      }
      document.getElementById('tabUploadText').innerText = t.tabUpload;
      document.getElementById('tabDownloadText').innerText = t.tabDownload;
      document.getElementById('tabTextLabel').innerText = t.tabText;
      document.getElementById('dropzoneTitle').innerText = t.dropTitle;
      document.getElementById('dropzoneSub').innerText = t.dropSub;
      document.getElementById('btnSelectText').innerText = t.btnSelect;
      document.getElementById('btnSelectFolderText').innerText = t.btnSelectFolder;
      document.getElementById('btnCameraText').innerText = t.btnCamera;
      document.getElementById('downloadAllText').innerText = t.downloadAll;
      document.getElementById('textInput').placeholder = t.textPlaceholder;
      document.getElementById('sendTextBtn').innerText = t.sendText;
      updateScopeBadge();
      if (sessionExpired) {
        document.getElementById('expiredBanner').innerText = t.expiredOneShot;
      }
      renderDownloadList(lastFiles);
      renderTextList(lastTexts);
      refreshUploadTexts();
      updateSoundBtn();
    }

    function updateUnreadBadges() {
      const dlBadge = document.getElementById('badgeDownload');
      const txtBadge = document.getElementById('badgeText');
      if (unreadFiles > 0) {
        dlBadge.innerText = unreadFiles > 99 ? '99+' : String(unreadFiles);
        dlBadge.classList.remove('hidden');
      } else {
        dlBadge.classList.add('hidden');
      }
      if (unreadTexts > 0) {
        txtBadge.innerText = unreadTexts > 99 ? '99+' : String(unreadTexts);
        txtBadge.classList.remove('hidden');
      } else {
        txtBadge.classList.add('hidden');
      }
    }

    function syncFilesAndUnread(files) {
      const incoming = files || [];
      if (seenFileIds === null) {
        seenFileIds = new Set(incoming.map((f) => f.id));
      } else {
        let added = 0;
        for (const f of incoming) {
          if (!seenFileIds.has(f.id)) {
            seenFileIds.add(f.id);
            added += 1;
          }
        }
        if (added > 0) {
          playSfx('msg');
          if (activeTab !== 'download') {
            unreadFiles += added;
            updateUnreadBadges();
          }
        }
      }
      renderDownloadList(incoming);
    }

    function syncTextsAndUnread(items, { countUnread = true } = {}) {
      const incoming = items || [];
      if (seenTextIds === null) {
        seenTextIds = new Set(incoming.map((m) => m.id));
      } else {
        let added = 0;
        for (const m of incoming) {
          if (!seenTextIds.has(m.id)) {
            seenTextIds.add(m.id);
            if (countUnread && m.sender === 'host') added += 1;
          }
        }
        if (added > 0) {
          playSfx('msg');
          if (activeTab !== 'text') {
            unreadTexts += added;
            updateUnreadBadges();
          }
        }
      }
      renderTextList(incoming);
    }

    function updateScopeBadge() {
      const t = I18N[currentLang];
      const badge = document.getElementById('scopeBadge');
      const labels = [];
      if (currentScope === 'receive_only') labels.push(t.scopeReceiveOnly);
      else if (currentScope === 'send_only') labels.push(t.scopeSendOnly);
      if (currentOneShot) labels.push(t.oneShotBadge);
      if (labels.length > 0) {
        badge.innerText = labels.join(' · ');
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
    }

    function applyScope(scope, oneShot) {
      currentScope = scope || 'both';
      currentOneShot = !!oneShot;
      const upBtn = document.getElementById('tabUploadBtn');
      const dlBtn = document.getElementById('tabDownloadBtn');
      const textForm = document.getElementById('textForm');
      upBtn.classList.toggle('hidden', currentScope === 'send_only');
      dlBtn.classList.toggle('hidden', currentScope === 'receive_only');
      textForm.classList.toggle('hidden', currentScope === 'send_only');
      if (currentScope === 'send_only' && activeTab === 'upload') switchTab('download');
      else if (currentScope === 'receive_only' && activeTab === 'download') switchTab('upload');
      updateScopeBadge();
    }

    function markExpired() {
      sessionExpired = true;
      const banner = document.getElementById('expiredBanner');
      banner.innerText = I18N[currentLang].expiredOneShot;
      banner.classList.remove('hidden');
      document.getElementById('sendTextBtn').disabled = true;
    }

    function switchTab(tab) {
      activeTab = tab;
      document.getElementById('uploadSection').classList.toggle('hidden', tab !== 'upload');
      document.getElementById('downloadSection').classList.toggle('hidden', tab !== 'download');
      document.getElementById('textSection').classList.toggle('hidden', tab !== 'text');
      document.getElementById('tabUploadBtn').classList.toggle('active', tab === 'upload');
      document.getElementById('tabDownloadBtn').classList.toggle('active', tab === 'download');
      document.getElementById('tabTextBtn').classList.toggle('active', tab === 'text');
      if (tab === 'download') {
        unreadFiles = 0;
        updateUnreadBadges();
      } else if (tab === 'text') {
        unreadTexts = 0;
        updateUnreadBadges();
      }
      if (tab !== 'upload') fetchInfo();
    }

    function formatBytes(bytes) {
      if (bytes === 0) return '0 B';
      const k = 1024;
      const sizes = ['B', 'KB', 'MB', 'GB'];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    async function fetchInfo() {
      try {
        const resp = await fetch('/api/info?token=' + encodeURIComponent(token));
        if (resp.status === 410) {
          markExpired();
          return;
        }
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.device_name) {
          peerDeviceName = data.device_name;
          const t = I18N[currentLang];
          document.getElementById('peerInfo').innerText = t.peer.replace('{name}', data.device_name);
        }
        applyScope(data.scope, data.one_shot);
        syncFilesAndUnread(data.files || []);
        if (Array.isArray(data.texts)) {
          syncTextsAndUnread(data.texts);
        }
      } catch (e) {
        console.error('Lỗi lấy thông tin phiên:', e);
      }
    }

    function appendLinkSafeText(parent, text) {
      const re = /https?:\\/\\/[^\\s<>"']+/g;
      let last = 0;
      let m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
        const a = document.createElement('a');
        a.href = m[0];
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = m[0];
        parent.appendChild(a);
        last = m.index + m[0].length;
      }
      if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }

    function copyTextValue(val, btn) {
      const done = () => {
        btn.innerText = I18N[currentLang].copied;
        setTimeout(() => { btn.innerText = I18N[currentLang].copy; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(val).then(done).catch(() => {});
        return;
      }
      const ta = document.createElement('textarea');
      ta.value = val;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (_) {}
      document.body.removeChild(ta);
    }

    function renderTextList(items) {
      lastTexts = items || [];
      const t = I18N[currentLang];
      const box = document.getElementById('textList');
      box.innerHTML = '';
      if (lastTexts.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.id = 'textEmpty';
        empty.innerText = t.emptyText;
        box.appendChild(empty);
        return;
      }
      [...lastTexts].reverse().forEach((item) => {
        const card = document.createElement('div');
        card.className = 'text-item';
        const head = document.createElement('div');
        head.className = 'text-item-head';
        const who = document.createElement('span');
        who.innerText = item.sender === 'host' ? t.fromHost : t.fromGuest;
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'btn-mini';
        copyBtn.innerText = t.copy;
        copyBtn.onclick = () => copyTextValue(item.text || '', copyBtn);
        head.appendChild(who);
        head.appendChild(copyBtn);
        const body = document.createElement('div');
        body.className = 'text-body';
        appendLinkSafeText(body, item.text || '');
        card.appendChild(head);
        card.appendChild(body);
        box.appendChild(card);
      });
    }

    async function sendText() {
      const inp = document.getElementById('textInput');
      const val = (inp.value || '').trim();
      if (!val || sessionExpired) return;
      try {
        const resp = await fetch('/api/text?token=' + encodeURIComponent(token), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: val })
        });
        if (resp.status === 410) { markExpired(); return; }
        if (resp.ok) {
          inp.value = '';
          const data = await resp.json();
          if (data.item && !lastTexts.some((x) => x.id === data.item.id)) {
            lastTexts.push(data.item);
            renderTextList(lastTexts);
          }
        }
      } catch (e) {
        console.error('Lỗi gửi văn bản:', e);
      }
    }

    function renderDownloadList(files) {
      lastFiles = files || [];
      const t = I18N[currentLang];
      const toolbar = document.getElementById('downloadToolbar');
      const dlAll = document.getElementById('downloadAllBtn');
      if (lastFiles.length >= 2) {
        toolbar.classList.remove('hidden');
        dlAll.href = '/api/download-zip?token=' + encodeURIComponent(token);
        dlAll.setAttribute('download', 'teleport-files.zip');
      } else {
        toolbar.classList.add('hidden');
      }

      const container = document.getElementById('downloadList');
      container.innerHTML = '';
      if (lastFiles.length === 0) {
        const emptyEl = document.createElement('div');
        emptyEl.className = 'empty-state';
        emptyEl.id = 'downloadEmpty';
        emptyEl.innerText = t.emptyDownload;
        container.appendChild(emptyEl);
        return;
      }

      lastFiles.forEach(f => {
        const item = document.createElement('div');
        item.className = 'file-item';

        if (f.is_image && !f.is_dir) {
          const thumb = document.createElement('img');
          thumb.className = 'file-thumb';
          thumb.loading = 'lazy';
          thumb.alt = '';
          thumb.src = '/api/download/' + encodeURIComponent(f.id) + '?token=' + encodeURIComponent(token) + '&inline=1';
          thumb.onerror = () => thumb.classList.add('hidden');
          item.appendChild(thumb);
        }

        const details = document.createElement('div');
        details.className = 'file-details';

        const nameEl = document.createElement('div');
        nameEl.className = 'file-name';
        nameEl.title = f.name;
        nameEl.innerText = f.name;

        const metaEl = document.createElement('div');
        metaEl.className = 'file-meta';
        metaEl.innerText = f.is_dir ? (t.folderTag + ' · ' + formatBytes(f.size)) : formatBytes(f.size);

        details.appendChild(nameEl);
        details.appendChild(metaEl);

        const link = document.createElement('a');
        link.className = 'btn-action';
        link.href = '/api/download/' + encodeURIComponent(f.id) + '?token=' + encodeURIComponent(token);
        link.setAttribute('download', f.is_dir ? (f.name + '.zip') : f.name);
        link.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>`;
        const btnText = document.createElement('span');
        btnText.innerText = f.is_dir ? (t.downloadBtn + ' (.zip)') : t.downloadBtn;
        link.appendChild(btnText);

        item.appendChild(details);
        item.appendChild(link);
        container.appendChild(item);
      });
    }

    const uploadQueue = [];
    let isUploading = false;
    let batchStartTime = 0;

    function formatEta(sec) {
      const s = Math.max(1, Math.round(sec));
      if (s < 60) return s + 's';
      const m = Math.floor(s / 60);
      const r = s % 60;
      return m + 'm ' + r + 's';
    }

    function updateBatchProgress() {
      const box = document.getElementById('batchBox');
      const active = uploadQueue.filter((e) => e.status !== 'canceled');
      if (active.length <= 1 && !active.some((e) => e.status === 'uploading' || e.status === 'queued')) {
        if (active.length === 0) box.classList.add('hidden');
        return;
      }
      box.classList.remove('hidden');
      let total = 0;
      let done = 0;
      for (const e of active) {
        total += e.total;
        done += e.status === 'done' ? e.total : e.loaded;
      }
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 100;
      document.getElementById('batchFill').style.width = pct + '%';
      const t = I18N[currentLang];
      document.getElementById('batchLabel').innerText = t.batchProgress
        .replace('{percent}', pct)
        .replace('{done}', formatBytes(done))
        .replace('{total}', formatBytes(total));
      const etaEl = document.getElementById('batchEta');
      const elapsed = batchStartTime > 0 ? (Date.now() - batchStartTime) / 1000 : 0;
      const speed = elapsed > 0.2 ? done / elapsed : 0;
      const remain = total - done;
      if (remain > 0 && speed > 0) {
        etaEl.innerText = t.etaLeft.replace('{eta}', formatEta(remain / speed));
      } else {
        etaEl.innerText = '';
      }
    }

    function createUploadEntry(file) {
      const relPath = file._relPath || file.webkitRelativePath || '';
      const displayName = relPath || file.name;
      const list = document.getElementById('uploadList');
      const item = document.createElement('div');
      item.className = 'file-item';

      const details = document.createElement('div');
      details.className = 'file-details';
      const nameEl = document.createElement('div');
      nameEl.className = 'file-name';
      nameEl.innerText = displayName;
      const metaEl = document.createElement('div');
      metaEl.className = 'file-meta';
      const progressBg = document.createElement('div');
      progressBg.className = 'progress-bar-bg';
      const progressFill = document.createElement('div');
      progressFill.className = 'progress-bar-fill';
      progressBg.appendChild(progressFill);
      details.appendChild(nameEl);
      details.appendChild(metaEl);
      details.appendChild(progressBg);

      const statusEl = document.createElement('div');
      statusEl.className = 'file-actions';
      item.appendChild(details);
      item.appendChild(statusEl);
      list.prepend(item);

      const entry = {
        file,
        relPath,
        status: 'queued',
        loaded: 0,
        total: file.size || 0,
        xhr: null,
        itemEl: item,
        metaEl,
        fillEl: progressFill,
        statusEl,
        lastErr: '',
      };
      renderEntryState(entry);
      return entry;
    }

    function renderEntryState(entry) {
      const t = I18N[currentLang];
      const sizeStr = formatBytes(entry.total);
      entry.statusEl.innerHTML = '';
      if (entry.status === 'queued') {
        entry.fillEl.style.width = '0%';
        entry.fillEl.style.background = 'var(--primary)';
        entry.metaEl.innerText = sizeStr + ' · ' + t.waiting;
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn-mini';
        cancelBtn.innerText = t.cancelBtn;
        cancelBtn.onclick = () => cancelUploadEntry(entry);
        entry.statusEl.appendChild(cancelBtn);
      } else if (entry.status === 'uploading') {
        entry.fillEl.style.background = 'var(--primary)';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn-mini';
        cancelBtn.innerText = t.cancelBtn;
        cancelBtn.onclick = () => cancelUploadEntry(entry);
        entry.statusEl.appendChild(cancelBtn);
      } else if (entry.status === 'done') {
        entry.fillEl.style.width = '100%';
        entry.fillEl.style.background = 'var(--success)';
        entry.metaEl.innerText = sizeStr + ' · ' + t.completed;
        entry.statusEl.innerHTML = '<span class="badge badge-success">✓</span>';
      } else if (entry.status === 'canceled' || entry.status === 'error') {
        entry.fillEl.style.background = entry.status === 'canceled' ? 'var(--text-muted)' : 'var(--danger)';
        const note = entry.status === 'canceled' ? t.canceled : t.failed.replace('{err}', entry.lastErr || '?');
        entry.metaEl.innerText = sizeStr + ' · ' + note;
        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'btn-mini';
        retryBtn.innerText = t.retryBtn;
        retryBtn.onclick = () => retryUploadEntry(entry);
        entry.statusEl.appendChild(retryBtn);
      }
      updateBatchProgress();
    }

    function refreshUploadTexts() {
      uploadQueue.forEach((entry) => {
        if (entry.status !== 'uploading') renderEntryState(entry);
      });
      updateBatchProgress();
    }

    function cancelUploadEntry(entry) {
      if (entry.status === 'uploading' && entry.xhr) {
        entry.status = 'canceled';
        entry.xhr.abort();
      } else if (entry.status === 'queued') {
        entry.status = 'canceled';
        renderEntryState(entry);
      }
    }

    function retryUploadEntry(entry) {
      entry.status = 'queued';
      entry.loaded = 0;
      entry.lastErr = '';
      renderEntryState(entry);
      processUploadQueue();
    }

    function handleFilesSelected(files) {
      if (!isUploading && !uploadQueue.some((e) => e.status === 'queued' || e.status === 'uploading')) {
        batchStartTime = Date.now();
      }
      for (let i = 0; i < files.length; i++) {
        uploadQueue.push(createUploadEntry(files[i]));
      }
      updateBatchProgress();
      processUploadQueue();
    }

    async function readEntryFiles(entry, prefix) {
      if (entry.isFile) {
        return new Promise((resolve) => {
          entry.file((file) => {
            file._relPath = prefix ? (prefix + '/' + file.name) : file.name;
            resolve([file]);
          }, () => resolve([]));
        });
      }
      if (entry.isDirectory) {
        const reader = entry.createReader();
        const nextPrefix = prefix ? (prefix + '/' + entry.name) : entry.name;
        const collected = [];
        while (true) {
          const batch = await new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
          if (!batch || batch.length === 0) break;
          for (const child of batch) {
            const sub = await readEntryFiles(child, nextPrefix);
            collected.push(...sub);
          }
        }
        return collected;
      }
      return [];
    }

    const dropZone = document.getElementById('dropZone');
    dropZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => {
      dropZone.classList.remove('dragover');
    });
    dropZone.addEventListener('drop', async (e) => {
      e.preventDefault();
      dropZone.classList.remove('dragover');
      const items = e.dataTransfer && e.dataTransfer.items;
      if (items && items.length > 0 && items[0].webkitGetAsEntry) {
        const droppedFiles = [];
        const entries = [];
        for (let i = 0; i < items.length; i++) {
          const entry = items[i].webkitGetAsEntry();
          if (entry) entries.push(entry);
        }
        for (const entry of entries) {
          const files = await readEntryFiles(entry, '');
          droppedFiles.push(...files);
        }
        if (droppedFiles.length > 0) {
          handleFilesSelected(droppedFiles);
          return;
        }
      }
      if (e.dataTransfer && e.dataTransfer.files) {
        handleFilesSelected(e.dataTransfer.files);
      }
    });

    async function processUploadQueue() {
      if (isUploading) return;
      const next = uploadQueue.find((e) => e.status === 'queued');
      if (!next) {
        updateBatchProgress();
        return;
      }
      isUploading = true;
      await uploadFile(next);
      isUploading = false;
      processUploadQueue();
    }

    function uploadFile(entry) {
      return new Promise((resolve) => {
        if (entry.status === 'canceled') {
          resolve();
          return;
        }
        entry.status = 'uploading';
        renderEntryState(entry);
        const file = entry.file;
        const relPath = entry.relPath;
        const xhr = new XMLHttpRequest();
        entry.xhr = xhr;
        const startTime = Date.now();

        xhr.upload.onprogress = function(e) {
          if (e.lengthComputable && entry.status === 'uploading') {
            entry.loaded = e.loaded;
            const percent = e.total > 0 ? Math.round((e.loaded / e.total) * 100) : 100;
            entry.fillEl.style.width = percent + '%';
            const elapsedSec = (Date.now() - startTime) / 1000;
            const speed = elapsedSec > 0 ? formatBytes(e.loaded / elapsedSec) + '/s' : '';
            entry.metaEl.innerText = formatBytes(entry.total) + ' · ' + I18N[currentLang].uploading
              .replace('{percent}', percent)
              .replace('{speed}', speed);
            updateBatchProgress();
          }
        };

        xhr.onload = function() {
          entry.xhr = null;
          if (xhr.status >= 200 && xhr.status < 300) {
            entry.loaded = entry.total;
            entry.status = 'done';
            playSfx('done');
          } else {
            entry.status = 'error';
            entry.lastErr = xhr.statusText || String(xhr.status);
            playSfx('err');
          }
          renderEntryState(entry);
          resolve();
        };

        xhr.onerror = function() {
          entry.xhr = null;
          if (entry.status !== 'canceled') {
            entry.status = 'error';
            entry.lastErr = 'Network';
            playSfx('err');
          }
          renderEntryState(entry);
          resolve();
        };

        xhr.onabort = function() {
          entry.xhr = null;
          entry.status = 'canceled';
          renderEntryState(entry);
          resolve();
        };

        let targetUrl = '/api/upload?name=' + encodeURIComponent(file.name) + '&token=' + encodeURIComponent(token);
        if (relPath && relPath.includes('/')) {
          targetUrl += '&rel_path=' + encodeURIComponent(relPath);
        }
        xhr.open('POST', targetUrl, true);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.send(file);
      });
    }

    function connectEvents() {
      if (!window.EventSource) return;
      const es = new EventSource('/api/events?token=' + encodeURIComponent(token));
      const onInit = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload.scope || typeof payload.one_shot === 'boolean') {
            applyScope(payload.scope, payload.one_shot);
          }
          if (payload.files) syncFilesAndUnread(payload.files);
          if (Array.isArray(payload.texts)) syncTextsAndUnread(payload.texts);
        } catch (_) {}
      };
      const onFiles = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload.files) syncFilesAndUnread(payload.files);
        } catch (_) {}
      };
      const onText = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload.item && !lastTexts.some((x) => x.id === payload.item.id)) {
            syncTextsAndUnread([...lastTexts, payload.item]);
          }
        } catch (_) {}
      };
      es.addEventListener('init', onInit);
      es.addEventListener('files_updated', onFiles);
      es.addEventListener('text_message', onText);
      es.addEventListener('session_expired', () => markExpired());
    }

    updateTexts();
    fetchInfo();
    connectEvents();
  </script>
</body>
</html>
"""
