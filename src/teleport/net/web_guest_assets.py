# ruff: noqa: E501, W293
"""Tài nguyên giao diện Web chế độ khách (Web Guest Mode) cho ZCL Teleport (v1.2.0).

Toàn bộ trang web được đóng gói trong một file HTML duy nhất (< 50 KB),
nhúng sẵn CSS và JavaScript thuần, hỗ trợ chế độ sáng/tối tự động và đa ngôn ngữ (VI/EN).
Không phụ thuộc bất kỳ thư viện hay CDN bên ngoài nào, hoạt động ngoại tuyến 100% trong mạng nội bộ.
"""

from __future__ import annotations

WEB_GUEST_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
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
      max-width: 520px;
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
      padding: 32px 20px;
      text-align: center;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      transition: border-color 0.2s;
    }
    .dropzone:active, .dropzone.dragover {
      border-color: var(--primary);
      background: rgba(2, 132, 199, 0.05);
    }
    .dropzone svg {
      width: 44px;
      height: 44px;
      stroke: var(--primary);
    }
    .btn-select {
      background: var(--primary);
      color: #ffffff;
      border: none;
      padding: 10px 20px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.95rem;
      cursor: pointer;
    }
    .file-list {
      margin-top: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
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
          <h1>ZCL Teleport</h1>
          <div class="peer-info" id="peerInfo">Kết nối nội bộ trực tiếp</div>
        </div>
      </div>
      <button class="lang-btn" id="langBtn" onclick="toggleLang()">EN</button>
    </header>

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
      </button>
    </div>

    <!-- Tab Gửi sang máy tính -->
    <div class="card" id="uploadSection">
      <input type="file" id="filePicker" multiple style="display:none;" onchange="handleFilesSelected(this.files)">
      <div class="dropzone" id="dropZone" onclick="document.getElementById('filePicker').click()">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"></path>
          <path d="M12 12v9"></path>
          <path d="m16 16-4-4-4 4"></path>
        </svg>
        <div>
          <strong id="dropzoneTitle">Chọn tệp hoặc ảnh từ điện thoại</strong>
          <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;" id="dropzoneSub">Bấm vào đây để chọn nhiều tệp cùng lúc</div>
        </div>
        <button class="btn-select" id="btnSelectText">Chọn tệp</button>
      </div>

      <div class="file-list" id="uploadList"></div>
    </div>

    <!-- Tab Nhận về điện thoại -->
    <div class="card hidden" id="downloadSection">
      <div class="file-list" id="downloadList">
        <div class="empty-state" id="downloadEmpty">Không có tệp nào đang chờ nhận từ máy tính.</div>
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
    let currentLang = 'vi';

    const I18N = {
      vi: {
        peer: 'Kết nối máy tính: {name}',
        tabUpload: 'Gửi sang máy tính',
        tabDownload: 'Nhận về điện thoại',
        dropTitle: 'Chọn tệp hoặc ảnh từ điện thoại',
        dropSub: 'Bấm vào đây để chọn nhiều tệp cùng lúc',
        btnSelect: 'Chọn tệp',
        emptyDownload: 'Không có tệp nào đang chờ nhận từ máy tính.',
        downloadBtn: 'Tải về',
        uploading: 'Đang gửi... {percent}% ({speed})',
        completed: 'Hoàn tất',
        failed: 'Thất bại: {err}',
        waiting: 'Đang chờ...',
      },
      en: {
        peer: 'Connected PC: {name}',
        tabUpload: 'Send to Computer',
        tabDownload: 'Receive on Phone',
        dropTitle: 'Select files or photos',
        dropSub: 'Tap here to choose files to send',
        btnSelect: 'Choose Files',
        emptyDownload: 'No files waiting to download from computer.',
        downloadBtn: 'Download',
        uploading: 'Sending... {percent}% ({speed})',
        completed: 'Finished',
        failed: 'Failed: {err}',
        waiting: 'Pending...',
      }
    };

    function toggleLang() {
      currentLang = currentLang === 'vi' ? 'en' : 'vi';
      document.getElementById('langBtn').innerText = currentLang === 'vi' ? 'EN' : 'VI';
      updateTexts();
    }

    function updateTexts() {
      const t = I18N[currentLang];
      document.getElementById('tabUploadText').innerText = t.tabUpload;
      document.getElementById('tabDownloadText').innerText = t.tabDownload;
      document.getElementById('dropzoneTitle').innerText = t.dropTitle;
      document.getElementById('dropzoneSub').innerText = t.dropSub;
      document.getElementById('btnSelectText').innerText = t.btnSelect;
      if (document.getElementById('downloadEmpty')) {
        document.getElementById('downloadEmpty').innerText = t.emptyDownload;
      }
    }

    function switchTab(tab) {
      if (tab === 'upload') {
        document.getElementById('uploadSection').classList.remove('hidden');
        document.getElementById('downloadSection').classList.add('hidden');
        document.getElementById('tabUploadBtn').classList.add('active');
        document.getElementById('tabDownloadBtn').classList.remove('active');
      } else {
        document.getElementById('uploadSection').classList.add('hidden');
        document.getElementById('downloadSection').classList.remove('hidden');
        document.getElementById('tabUploadBtn').classList.remove('active');
        document.getElementById('tabDownloadBtn').classList.add('active');
        fetchInfo();
      }
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
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.device_name) {
          const t = I18N[currentLang];
          document.getElementById('peerInfo').innerText = t.peer.replace('{name}', data.device_name);
        }
        renderDownloadList(data.files || []);
      } catch (e) {
        console.error('Lỗi lấy thông tin phiên:', e);
      }
    }

    function renderDownloadList(files) {
      const container = document.getElementById('downloadList');
      if (!files || files.length === 0) {
        container.innerHTML = '<div class="empty-state" id="downloadEmpty">' + I18N[currentLang].emptyDownload + '</div>';
        return;
      }
      container.innerHTML = '';
      files.forEach(f => {
        const item = document.createElement('div');
        item.className = 'file-item';
        item.innerHTML = `
          <div class="file-details">
            <div class="file-name" title="${f.name}">${f.name}</div>
            <div class="file-meta">${formatBytes(f.size)}</div>
          </div>
          <a class="btn-action" href="/api/download/${f.id}?token=${encodeURIComponent(token)}" download="${f.name}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            ${I18N[currentLang].downloadBtn}
          </a>
        `;
        container.appendChild(item);
      });
    }

    const uploadQueue = [];
    let isUploading = false;

    function handleFilesSelected(files) {
      for (let i = 0; i < files.length; i++) {
        uploadQueue.push(files[i]);
      }
      processUploadQueue();
    }

    async function processUploadQueue() {
      if (isUploading || uploadQueue.length === 0) return;
      isUploading = true;
      const file = uploadQueue.shift();
      await uploadFile(file);
      isUploading = false;
      processUploadQueue();
    }

    function uploadFile(file) {
      return new Promise((resolve) => {
        const list = document.getElementById('uploadList');
        const item = document.createElement('div');
        item.className = 'file-item';
        
        const details = document.createElement('div');
        details.className = 'file-details';
        
        const nameEl = document.createElement('div');
        nameEl.className = 'file-name';
        nameEl.innerText = file.name;
        
        const metaEl = document.createElement('div');
        metaEl.className = 'file-meta';
        metaEl.innerText = formatBytes(file.size) + ' · ' + I18N[currentLang].waiting;

        const progressBg = document.createElement('div');
        progressBg.className = 'progress-bar-bg';
        const progressFill = document.createElement('div');
        progressFill.className = 'progress-bar-fill';
        progressBg.appendChild(progressFill);

        details.appendChild(nameEl);
        details.appendChild(metaEl);
        details.appendChild(progressBg);

        const statusEl = document.createElement('div');
        item.appendChild(details);
        item.appendChild(statusEl);
        list.prepend(item);

        const xhr = new XMLHttpRequest();
        let startTime = Date.now();
        let lastLoaded = 0;

        xhr.upload.onprogress = function(e) {
          if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = percent + '%';
            const elapsedSec = (Date.now() - startTime) / 1000;
            const speed = elapsedSec > 0 ? formatBytes(e.loaded / elapsedSec) + '/s' : '';
            metaEl.innerText = formatBytes(file.size) + ' · ' + I18N[currentLang].uploading
              .replace('{percent}', percent)
              .replace('{speed}', speed);
          }
        };

        xhr.onload = function() {
          if (xhr.status >= 200 && xhr.status < 300) {
            progressFill.style.width = '100%';
            progressFill.style.background = 'var(--success)';
            metaEl.innerText = formatBytes(file.size) + ' · ' + I18N[currentLang].completed;
            statusEl.innerHTML = '<span class="badge badge-success">✓</span>';
          } else {
            progressFill.style.background = 'var(--danger)';
            metaEl.innerText = formatBytes(file.size) + ' · ' + I18N[currentLang].failed.replace('{err}', xhr.statusText || xhr.status);
            statusEl.innerHTML = '<span class="badge badge-danger">✕</span>';
          }
          resolve();
        };

        xhr.onerror = function() {
          progressFill.style.background = 'var(--danger)';
          metaEl.innerText = formatBytes(file.size) + ' · ' + I18N[currentLang].failed.replace('{err}', 'Mất kết nối');
          statusEl.innerHTML = '<span class="badge badge-danger">✕</span>';
          resolve();
        };

        const targetUrl = '/api/upload?name=' + encodeURIComponent(file.name) + '&token=' + encodeURIComponent(token);
        xhr.open('POST', targetUrl, true);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.send(file);
      });
    }

    fetchInfo();
  </script>
</body>
</html>
"""
