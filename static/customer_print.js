/* QR Se Print – customer page logic
   Globals injected by the template: SHOP_ID, SHOP_CODE, BKASH_NUM, NAGAD_NUM, RATES */
(function () {
  const $ = (id) => document.getElementById(id);
  window.$ = $;
  const MAX_BYTES = 50 * 1024 * 1024;
  const ACTIVE_TAB = 'py-2 rounded-xl font-bold text-[11px] sm:text-xs transition bg-pink-600 text-white shadow';
  const IDLE_TAB = 'py-2 rounded-xl font-bold text-[11px] sm:text-xs transition text-gray-600 hover:text-gray-900';
  const bn = (n) => String(n).replace(/\d/g, (d) => '০১২৩৪৫৬৭৮৯'[d]);

  let currentMode = 'document'; // 'document' | 'nid' | 'studio' | 'transfer'
  let uploadedFile = null;
  let nidFrontFile = null;
  let nidBackFile = null;
  let nidRot = { front: 0, back: 0 };

  // ── helpers ──────────────────────────────────────────────────────────────
  function sendForm(url, formData, onProgress) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        let json = {};
        try { json = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
        resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, json });
      };
      xhr.onerror = () => resolve({ ok: false, status: 0, json: { detail: 'সার্ভারে সংযোগ পাওয়া যায়নি।' } });
      xhr.send(formData);
    });
  }

  function setBar(bar, label, p) {
    bar.style.width = p + '%';
    label.innerText = p + '%';
  }

  function checked(name) {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : null;
  }

  function fmtSize(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(0) + ' KB';
    return (b / 1048576).toFixed(1) + ' MB';
  }

  // ── elements ─────────────────────────────────────────────────────────────
  const tabs = { document: $('tabDocMode'), nid: $('tabNidMode'), studio: $('tabStudioMode'), transfer: $('tabTransferMode') };
  const studioCard = $('studioCard');
  const printFlow = $('printFlow');
  const transferFlow = $('transferFlow');
  const dropzoneNormal = $('dropzoneNormal');
  const dropzoneNid = $('dropzoneNid');
  const step1Title = $('step1Title');
  const paperTypeWrapper = $('paperTypeWrapper');
  const nupWrapper = $('nupWrapper');
  const duplexWrapper = $('duplexWrapper');
  const nidFitWrapper = $('nidFitWrapper');
  const optPhoto = $('optPhoto');
  const settingsCard = $('settingsCard');
  const paymentCard = $('paymentCard');
  const fileInput = $('fileInput');
  const uploadProgress = $('uploadProgress');
  const progressBar = $('progressBar');
  const progressPercent = $('progressPercent');
  const progressLabel = $('progressLabel');
  const fileInfoBadge = $('fileInfoBadge');
  const inputCopies = $('inputCopies');
  const totalPriceText = $('totalPriceText');
  const billSummaryText = $('billSummaryText');
  const btnMergeNid = $('btnMergeNid');

  const STORAGE_KEY = 'qsp_state_' + (typeof SHOP_CODE !== 'undefined' ? SHOP_CODE : 'default');
  const RECENT_ORDERS_KEY = 'qsp_recent_orders_' + (typeof SHOP_CODE !== 'undefined' ? SHOP_CODE : 'default');
  let fileRev = 1;
  let instantLocalUrl = null;
  let docInlinePage = 1;

  function syncActiveStudioPhotoUrl() {
    if (uploadedFile && uploadedFile.file_id && Array.isArray(studioPhotos)) {
      const match = studioPhotos.find(p => p.file_id === uploadedFile.file_id);
      if (match) {
        match.url = instantLocalUrl || `/api/preview/${encodeURIComponent(match.file_id)}?w=300&v=${fileRev}`;
        renderStudioPhotoPool();
      }
    }
  }

  function bumpFileRev() {
    fileRev++;
    docInlinePage = 1;
    if (instantLocalUrl) {
      URL.revokeObjectURL(instantLocalUrl);
      instantLocalUrl = null;
    }
    syncActiveStudioPhotoUrl();
  }

  // Fast client-side image compression before upload (shrinks 5-12MB camera photos to ~300KB in <60ms)
  function compressImageBeforeUpload(file, maxDim = 2000, quality = 0.86) {
    return new Promise((resolve) => {
      if (!file || !/^image\/(jpeg|png|webp)$/i.test(file.type) || file.size < 350 * 1024) {
        return resolve(file);
      }
      const url = URL.createObjectURL(file);
      const im = new Image();
      im.onload = () => {
        URL.revokeObjectURL(url);
        let { width, height } = im;
        if (width <= maxDim && height <= maxDim && file.size < 700 * 1024) {
          return resolve(file);
        }
        if (width > maxDim || height > maxDim) {
          const scale = maxDim / Math.max(width, height);
          width = Math.round(width * scale);
          height = Math.round(height * scale);
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, width, height);
        ctx.drawImage(im, 0, 0, width, height);
        canvas.toBlob((blob) => {
          if (!blob || blob.size >= file.size) return resolve(file);
          const cleanName = (file.name || 'photo.jpg').replace(/\.[^.]+$/, '') + '.jpg';
          resolve(new File([blob], cleanName, { type: 'image/jpeg' }));
        }, 'image/jpeg', quality);
      };
      im.onerror = () => {
        URL.revokeObjectURL(url);
        resolve(file);
      };
      im.src = url;
    });
  }

  function saveSessionState() {
    try {
      if (!uploadedFile) {
        localStorage.removeItem(STORAGE_KEY);
        return;
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        mode: currentMode,
        uploadedFile: uploadedFile,
        nidRot: nidRot
      }));
    } catch (e) { /* ignore storage quota errors */ }
  }

  async function restoreSessionState() {
    if (location.hash === '#transfer') return;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const state = JSON.parse(raw);
      if (!state || !state.uploadedFile || !state.uploadedFile.file_id) return;

      const res = await fetch(`/api/file/check/${encodeURIComponent(state.uploadedFile.file_id)}?t=${Date.now()}`);
      if (!res.ok) return;
      const check = await res.json();
      if (!check.exists) {
        localStorage.removeItem(STORAGE_KEY);
        return;
      }

      uploadedFile = state.uploadedFile;
      nidRot = state.nidRot || { front: 0, back: 0 };
      const targetMode = state.mode && state.mode !== 'transfer' ? state.mode : (uploadedFile.is_nid ? 'nid' : 'document');
      setMode(targetMode, true);
      showUploadedFile();
    } catch (e) { /* ignore */ }
  }

  function refreshTopLivePreview(forceReloadImg = false) {
    const docPrev = $('docInlinePreview');
    const nidPrev = $('nidInlinePreview');
    if (!uploadedFile) {
      if (docPrev) docPrev.classList.add('hidden');
      if (nidPrev) nidPrev.classList.add('hidden');
      return;
    }

    if (uploadedFile.is_nid) {
      if (docPrev) docPrev.classList.add('hidden');
      if (nidPrev) nidPrev.classList.remove('hidden');
      refreshNidInline();
      return;
    }

    if (nidPrev) nidPrev.classList.add('hidden');
    if (!docPrev) return;
    docPrev.classList.remove('hidden');

    const img = $('docInlineImg');
    const loader = $('docInlineLoading');
    const pageNav = $('docInlinePageNav');
    const pageSel = $('docInlinePageSelect');
    const btnPrev = $('btnDocPrevPage');
    const btnNext = $('btnDocNextPage');
    const badge = $('docInlineModeBadge');
    const sheet = $('docPaperSheet');
    const orientNote = $('docPaperOrientNote');
    const isBw = currentMode === 'studio'
      ? ($('studioBw') && $('studioBw').value === '1')
      : ((checked('color_mode') || 'bw') === 'bw');
    const orient = checked('orientation') || 'portrait';
    const colorParam = isBw ? 'bw' : 'color';

    if (sheet) {
      if (currentMode === 'studio') {
        sheet.className = 'w-full max-w-[300px] bg-white rounded shadow-md border border-gray-300 p-2 flex items-center justify-center transition-all duration-300 overflow-hidden';
        if (orientNote) orientNote.innerText = '📸 ফটো স্টুডিওর জন্য সিলেক্ট করা ছবি';
      } else if (orient === 'landscape') {
        sheet.className = 'w-full max-w-[340px] aspect-[297/210] bg-white rounded shadow-md border-2 border-pink-300 p-2.5 flex items-center justify-center transition-all duration-300 overflow-hidden';
        if (orientNote) orientNote.innerText = '🖥️ A4 আড়াআড়ি (Landscape) পেজ লেআউট';
      } else {
        sheet.className = 'w-[220px] sm:w-[250px] aspect-[210/297] bg-white rounded shadow-md border-2 border-pink-300 p-2.5 flex items-center justify-center transition-all duration-300 overflow-hidden';
        if (orientNote) orientNote.innerText = '📱 A4 লম্বালম্বি (Portrait) পেজ লেআউট';
      }
    }

    if (badge) {
      if (currentMode === 'studio') {
        badge.innerText = isBw ? 'ফটো স্টুডিও • সাদাকালো' : 'ফটো স্টুডিও • রঙিন';
      } else {
        const orientBn = orient === 'landscape' ? 'আড়াআড়ি (Landscape)' : 'লম্বালম্বি (Portrait)';
        badge.innerText = `${isBw ? 'সাদাকালো (B&W)' : 'রঙিন (Color)'} • ${orientBn}`;
      }
      badge.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold ' +
        (isBw ? 'bg-gray-200 text-gray-800' : 'bg-pink-100 text-pink-700');
    }

    if (img) {
      if (uploadedFile.file_type === 'image') {
        if (pageNav) pageNav.classList.add('hidden');
        if (loader) loader.classList.add('hidden');
        img.style.filter = isBw ? 'grayscale(100%)' : 'none';
        const revKey = `${uploadedFile.file_id}_${fileRev}_${orient}_${colorParam}`;
        if (forceReloadImg || img.dataset.revKey !== revKey) {
          img.dataset.revKey = revKey;
          if (currentMode === 'studio') {
            img.src = instantLocalUrl || `/api/preview/${encodeURIComponent(uploadedFile.file_id)}?w=900&v=${fileRev}`;
          } else {
            // Document mode A4 sheet preview with exact image location
            img.src = `/api/preview-page/${encodeURIComponent(uploadedFile.file_id)}?page=1&paper_size=A4&color=${colorParam}&orientation=${orient}&v=${fileRev}`;
          }
        }
      } else {
        const nup = parseInt(checked('pages_per_sheet') || '1', 10);
        const totalSheets = Math.max(1, Math.ceil((uploadedFile.page_count || 1) / nup));
        if (docInlinePage > totalSheets) docInlinePage = totalSheets;
        if (docInlinePage < 1) docInlinePage = 1;

        if (pageNav) {
          pageNav.classList.toggle('hidden', totalSheets <= 1);
          if (pageSel) {
            if (pageSel.options.length !== totalSheets) {
              pageSel.innerHTML = '';
              for (let p = 1; p <= totalSheets; p++) {
                const opt = document.createElement('option');
                opt.value = String(p);
                opt.textContent = `${bn(p)} / ${bn(totalSheets)}`;
                pageSel.appendChild(opt);
              }
            }
            pageSel.value = String(docInlinePage);
          }
          if (btnPrev) btnPrev.disabled = docInlinePage <= 1;
          if (btnNext) btnNext.disabled = docInlinePage >= totalSheets;
        }

        const pdfKey = `${uploadedFile.file_id}_${fileRev}_${colorParam}_${orient}_p${docInlinePage}_n${nup}`;
        img.style.filter = 'none';
        if (forceReloadImg || img.dataset.revKey !== pdfKey) {
          img.dataset.revKey = pdfKey;
          if (loader) loader.classList.remove('hidden');
          img.onload = () => { if (loader) loader.classList.add('hidden'); };
          img.onerror = () => { if (loader) loader.classList.add('hidden'); };
          img.src = `/api/preview-page/${encodeURIComponent(uploadedFile.file_id)}?page=${docInlinePage}&nup=${nup}&color=${colorParam}&orientation=${orient}&v=${fileRev}`;
        }
      }
    }
  }

  // Wire multi-page controls in #docInlinePreview
  if ($('btnDocPrevPage')) {
    $('btnDocPrevPage').addEventListener('click', () => {
      if (docInlinePage > 1) {
        docInlinePage--;
        refreshTopLivePreview();
      }
    });
  }
  if ($('btnDocNextPage')) {
    $('btnDocNextPage').addEventListener('click', () => {
      if (!uploadedFile) return;
      const nup = parseInt(checked('pages_per_sheet') || '1', 10);
      const totalSheets = Math.max(1, Math.ceil((uploadedFile.page_count || 1) / nup));
      if (docInlinePage < totalSheets) {
        docInlinePage++;
        refreshTopLivePreview();
      }
    });
  }
  if ($('docInlinePageSelect')) {
    $('docInlinePageSelect').addEventListener('change', (e) => {
      docInlinePage = parseInt(e.target.value, 10) || 1;
      refreshTopLivePreview();
    });
  }

  // ── mode switching ───────────────────────────────────────────────────────
  function setMode(mode, keepFile = false) {
    currentMode = mode;
    Object.keys(tabs).forEach((k) => (tabs[k].className = k === mode ? ACTIVE_TAB : IDLE_TAB));
    const isTransfer = mode === 'transfer';
    printFlow.classList.toggle('hidden', isTransfer);
    transferFlow.classList.toggle('hidden', !isTransfer);
    if (isTransfer) return;

    const isNid = mode === 'nid';
    const isStudio = mode === 'studio';
    step1Title.innerText = isNid ? 'NID / ID কার্ডের ২ পিঠের ছবি দিন'
      : isStudio ? 'যে ছবিটি দিয়ে ফটো বানাবেন সেটি দিন' : 'ডকুমেন্ট বা ছবি সিলেক্ট করুন';
    fileInput.accept = isStudio ? 'image/png,image/jpeg,image/webp' : '.pdf,image/png,image/jpeg,image/webp';
    paperTypeWrapper.classList.add('hidden');            // photo sheets now live in the Photo Studio tab
    nupWrapper.classList.toggle('hidden', isNid || isStudio);
    duplexWrapper.classList.toggle('hidden', isNid || isStudio);
    nidFitWrapper.classList.toggle('hidden', !isNid);
    settingsCard.classList.toggle('hidden', isStudio);
    studioCard.classList.toggle('hidden', !isStudio);

    // Preserve uploaded photo across document <-> studio switch or page refresh unless incompatible
    const canKeep = keepFile || (
      uploadedFile &&
      !isNid &&
      !uploadedFile.is_nid &&
      (mode === 'document' || (mode === 'studio' && uploadedFile.file_type === 'image'))
    );

    if (canKeep && uploadedFile) {
      dropzoneNormal.classList.add('hidden');
      dropzoneNid.classList.add('hidden');
      saveSessionState();
      showUploadedFile();
    } else {
      dropzoneNormal.classList.toggle('hidden', isNid);
      dropzoneNid.classList.toggle('hidden', !isNid);
      resetUploads(false);
    }
  }
  tabs.document.addEventListener('click', () => setMode('document'));
  tabs.nid.addEventListener('click', () => setMode('nid'));
  tabs.studio.addEventListener('click', () => setMode('studio'));
  tabs.transfer.addEventListener('click', () => setMode('transfer'));

  function resetUploads(clearStorage = true) {
    uploadedFile = null;
    nidFrontFile = null;
    nidBackFile = null;
    nidRot = { front: 0, back: 0 };
    if (clearStorage) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
    }
    fileInput.value = '';
    $('nidFrontInput').value = '';
    $('nidBackInput').value = '';
    ['Front', 'Back'].forEach((side) => {
      $('nid' + side + 'Label').innerText = 'ছবি তুলুন/বাছুন';
      $('nid' + side + 'Thumb').classList.add('hidden');
      $('nid' + side + 'Icon').classList.remove('hidden');
    });
    btnMergeNid.disabled = true;
    btnMergeNid.innerText = 'দুটো ছবি সিলেক্ট করে NID শিট তৈরি করুন';
    btnMergeNid.className = 'w-full py-2.5 bg-gray-200 text-gray-500 font-bold text-xs rounded-xl transition cursor-not-allowed';
    fileInfoBadge.classList.add('hidden');
    $('nidInlinePreview').classList.add('hidden');
    if ($('docInlinePreview')) $('docInlinePreview').classList.add('hidden');
    optPhoto.classList.remove('hidden');
    settingsCard.classList.add('opacity-60', 'pointer-events-none');
    studioCard.classList.add('opacity-60', 'pointer-events-none');
    paymentCard.classList.add('opacity-60', 'pointer-events-none');
    totalPriceText.innerText = '৳ 0';
  }

  // ── NID: choose two photos ───────────────────────────────────────────────
  $('nidFrontBox').addEventListener('click', () => $('nidFrontInput').click());
  $('nidBackBox').addEventListener('click', () => $('nidBackInput').click());

  function onNidPicked(side, file) {
    if (!file) return;
    if (side === 'Front') nidFrontFile = file; else nidBackFile = file;
    $('nid' + side + 'Label').innerText = '✓ ' + file.name.substring(0, 14);
    const thumb = $('nid' + side + 'Thumb');
    thumb.src = URL.createObjectURL(file);
    thumb.classList.remove('hidden');
    $('nid' + side + 'Icon').classList.add('hidden');
    if (nidFrontFile && nidBackFile) {
      btnMergeNid.disabled = false;
      btnMergeNid.className = 'w-full py-2.5 bg-pink-600 hover:bg-pink-700 text-white font-bold text-xs rounded-xl transition shadow cursor-pointer';
      btnMergeNid.innerText = '✨ প্রিন্ট শিট তৈরি করুন ও প্রিভিউ দেখুন';
    }
  }
  $('nidFrontInput').addEventListener('change', (e) => onNidPicked('Front', e.target.files[0]));
  $('nidBackInput').addEventListener('change', (e) => onNidPicked('Back', e.target.files[0]));

  btnMergeNid.addEventListener('click', async () => {
    if (!nidFrontFile || !nidBackFile) return;
    dropzoneNid.classList.add('hidden');
    progressLabel.innerText = 'NID ছবি আপলোড ও প্রসেসিং হচ্ছে...';
    setBar(progressBar, progressPercent, 0);
    uploadProgress.classList.remove('hidden');

    const [compFront, compBack] = await Promise.all([
      compressImageBeforeUpload(nidFrontFile, 1800, 0.86),
      compressImageBeforeUpload(nidBackFile, 1800, 0.86)
    ]);
    const fd = new FormData();
    fd.append('front', compFront);
    fd.append('back', compBack);
    const res = await sendForm('/api/upload-nid', fd, (p) => setBar(progressBar, progressPercent, p));
    uploadProgress.classList.add('hidden');
    if (res.ok) {
      bumpFileRev();
      uploadedFile = res.json;
      showUploadedFile();
    } else {
      alert(res.json.detail || 'NID ছবি প্রসেসিং ব্যর্থ হয়েছে।');
      dropzoneNid.classList.remove('hidden');
    }
  });

  function nidPreviewUrl(width) {
    const orient = checked('orientation') || 'portrait';
    return `/api/nid-preview/${uploadedFile.nid_token}?color=${checked('color_mode') || 'bw'}` +
      `&fit=${checked('nid_fit') || 'cover'}&rf=${nidRot.front}&rb=${nidRot.back}` +
      `&orientation=${orient}&w=${width}&t=${Date.now()}`;
  }
  let nidTimer = null;
  function refreshNidInline() {
    if (!uploadedFile || !uploadedFile.is_nid) return;
    clearTimeout(nidTimer);
    nidTimer = setTimeout(() => { $('nidInlineImg').src = nidPreviewUrl(800); }, 150);
  }
  $('btnRotFront').addEventListener('click', () => { nidRot.front = (nidRot.front + 90) % 360; saveSessionState(); refreshNidInline(); });
  $('btnRotBack').addEventListener('click', () => { nidRot.back = (nidRot.back + 90) % 360; saveSessionState(); refreshNidInline(); });

  // ── normal file upload ───────────────────────────────────────────────────
  dropzoneNormal.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (e) => { if (e.target.files.length) handleFileUpload(e.target.files[0]); });
  dropzoneNormal.addEventListener('dragover', (e) => { e.preventDefault(); dropzoneNormal.classList.add('border-pink-500'); });
  dropzoneNormal.addEventListener('dragleave', () => dropzoneNormal.classList.remove('border-pink-500'));
  dropzoneNormal.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzoneNormal.classList.remove('border-pink-500');
    if (e.dataTransfer.files.length) handleFileUpload(e.dataTransfer.files[0]);
  });

  async function handleFileUpload(file) {
    if (currentMode === 'studio' && !/^image\//.test(file.type)) { alert('ফটো স্টুডিওতে শুধু ছবি (JPG/PNG) দিন।'); return; }
    if (file.size > MAX_BYTES) { alert('ফাইলের সাইজ সর্বোচ্চ ৫০ MB হতে পারবে।'); return; }
    dropzoneNormal.classList.add('hidden');

    // Show 0ms instant local preview for images while uploading
    bumpFileRev();
    let uploadTarget = file;
    if (/^image\//i.test(file.type)) {
      instantLocalUrl = URL.createObjectURL(file);
      const docPrev = $('docInlinePreview');
      const docImg = $('docInlineImg');
      if (docPrev && docImg) {
        docPrev.classList.remove('hidden');
        docImg.src = instantLocalUrl;
      }
      uploadTarget = await compressImageBeforeUpload(file, 2000, 0.86);
    }

    progressLabel.innerText = 'ফাইল আপলোড ও প্রসেসিং হচ্ছে...';
    setBar(progressBar, progressPercent, 0);
    uploadProgress.classList.remove('hidden');

    const fd = new FormData();
    fd.append('file', uploadTarget);
    const res = await sendForm('/api/upload', fd, (p) => setBar(progressBar, progressPercent, p));
    uploadProgress.classList.add('hidden');
    if (res.ok) {
      uploadedFile = res.json;
      showUploadedFile();
    } else {
      alert(res.json.detail || 'ফাইল আপলোড ব্যর্থ হয়েছে।');
      dropzoneNormal.classList.remove('hidden');
      fileInput.value = '';
    }
  }

  function showUploadedFile() {
    saveSessionState();
    dropzoneNormal.classList.add('hidden');
    dropzoneNid.classList.add('hidden');
    $('fileNameText').innerText = uploadedFile.original_filename;
    const isImg = uploadedFile.file_type === 'image';
    const isPdf = uploadedFile.file_type === 'pdf';
    const iconEl = $('fileIcon');
    if (iconEl) {
      if (uploadedFile.doc_kind === 'excel') iconEl.innerText = '📊';
      else if (uploadedFile.doc_kind === 'word') iconEl.innerText = '📝';
      else if (isImg) iconEl.innerText = '🖼️';
      else iconEl.innerText = '📄';
    }
    $('fileMetaText').innerText = uploadedFile.is_nid
      ? 'NID কার্ড — A4 পেজে সামনে ও পেছনে (আসল মাপ ৮৫.৬×৫৪ মিমি)'
      : `মোট পেজ: ${bn(uploadedFile.page_count)} টি${uploadedFile.is_office ? ' (এডিটযোগ্য)' : ''}`;
    fileInfoBadge.classList.remove('hidden');

    const canCropOrScan = isImg || isPdf;
    const btnCrop = $('btnOpenCrop');
    const btnCam = $('btnOpenCamScan');
    const btnRbg = $('btnOpenRemoveBg');
    const btnOfficeEdit = $('btnOpenOfficeEdit');
    const btnImgPos = $('btnOpenImagePosition');
    const barImgPos = $('imgPosQuickBar');
    const isSingleImgA4 = isImg && !uploadedFile.is_nid && currentMode !== 'studio';

    if (btnCrop) btnCrop.classList.toggle('hidden', !canCropOrScan);
    if (btnCam) btnCam.classList.toggle('hidden', !canCropOrScan);
    if (btnRbg) btnRbg.classList.toggle('hidden', !isImg);
    if (btnOfficeEdit) btnOfficeEdit.classList.toggle('hidden', !isPdf || !!uploadedFile.is_nid);
    if (btnImgPos) btnImgPos.classList.toggle('hidden', !isSingleImgA4);
    if (barImgPos) barImgPos.classList.toggle('hidden', !isSingleImgA4);

    if (isSingleImgA4) {
      loadImagePositionConfig(uploadedFile.file_id);
    }

    if (uploadedFile.office_orientation === 'landscape') {
      selectRadio('optLandscape');
    }

    refreshTopLivePreview();

    if (currentMode === 'studio') {
      studioCard.classList.remove('opacity-60', 'pointer-events-none');
      paymentCard.classList.remove('opacity-60', 'pointer-events-none');
      initStudio();
      calculateBill();
      return;
    }

    // 4x6 passport sheet only makes sense for images; N-up only for PDFs
    optPhoto.classList.toggle('hidden', isPdf);
    if (isPdf) selectRadio('optDocA4');
    nupWrapper.classList.toggle('hidden', !isPdf || uploadedFile.is_nid);

    settingsCard.classList.remove('opacity-60', 'pointer-events-none');
    paymentCard.classList.remove('opacity-60', 'pointer-events-none');
    calculateBill();
  }

  $('btnRemoveFile').addEventListener('click', () => {
    resetUploads();
    (currentMode === 'nid' ? dropzoneNid : dropzoneNormal).classList.remove('hidden');
  });

  // ── option radio groups ──────────────────────────────────────────────────
  const GROUP_IDS = [];
  function setupRadioGroup(groupIds, onChange) {
    GROUP_IDS.push(groupIds);
    groupIds.forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('click', () => {
        selectRadio(id);
        if (onChange) onChange();
      });
    });
  }
  function selectRadio(id) {
    const group = GROUP_IDS.find((g) => g.includes(id));
    if (!group) return;
    group.forEach((otherId) => {
      const o = $(otherId);
      if (o) {
        o.classList.remove('border-pink-600', 'bg-pink-50', 'text-pink-900');
        o.classList.add('border-gray-200', 'text-gray-700');
      }
    });
    const el = $(id);
    el.classList.add('border-pink-600', 'bg-pink-50', 'text-pink-900');
    el.classList.remove('border-gray-200', 'text-gray-700');
    el.querySelector('input').checked = true;
  }

  setupRadioGroup(['optDocA4', 'optPhoto'], () => {
    const isPhoto = checked('paper_size') === '4x6';
    duplexWrapper.style.display = isPhoto ? 'none' : '';
    nupWrapper.style.display = isPhoto ? 'none' : '';
    calculateBill();
  });
  setupRadioGroup(['optNup1', 'optNup2', 'optNup4'], () => { docInlinePage = 1; calculateBill(); refreshTopLivePreview(); });
  setupRadioGroup(['optBw', 'optColor'], () => { calculateBill(); refreshTopLivePreview(); });
  setupRadioGroup(['optSingle', 'optDouble'], calculateBill);
  setupRadioGroup(['optPortrait', 'optLandscape'], () => { calculateBill(); refreshTopLivePreview(); });
  setupRadioGroup(['optFitCover', 'optFitContain'], refreshNidInline);
  setupRadioGroup(['payMethodCash', 'payMethodBkash', 'payMethodNagad'], () => {
    const method = checked('payment_method');
    const box = $('mfsDetailsBox');
    if (method === 'bkash') {
      box.classList.remove('hidden');
      $('mfsInstruction').innerText = 'দোকানের বিকাশ নম্বরে সেন্ড মানি করুন:';
      $('mfsNumber').innerText = BKASH_NUM;
    } else if (method === 'nagad') {
      box.classList.remove('hidden');
      $('mfsInstruction').innerText = 'দোকানের নগদ নম্বরে সেন্ড মানি করুন:';
      $('mfsNumber').innerText = NAGAD_NUM;
    } else {
      box.classList.add('hidden');
    }
  });

  $('btnMinusCopy').addEventListener('click', () => {
    const v = parseInt(inputCopies.value, 10);
    if (v > 1) { inputCopies.value = v - 1; calculateBill(); }
  });
  $('btnPlusCopy').addEventListener('click', () => {
    const v = parseInt(inputCopies.value, 10);
    if (v < 100) { inputCopies.value = v + 1; calculateBill(); }
  });

  function calculateBill() {
    if (!uploadedFile) return;
    const copies = parseInt(inputCopies.value, 10);
    const color = checked('color_mode') || 'bw';
    const colorBn = color === 'bw' ? 'সাদাকালো' : 'রঙিন';
    let total = 0;

    if (currentMode === 'studio') {
      const sp = studioParams();
      const rate = sp.paper === '4x6' ? RATES.photo_4x6 : (sp.bw ? RATES.bw_single : RATES.photo_a4);
      const sc = parseInt($('studioCopies').value, 10);
      total = rate * sc;
      billSummaryText.innerText = `ফটো শিট (${sp.paper === '4x6' ? '৪×৬' : 'A4'}) × ${bn(sc)} কপি`;
    } else if (uploadedFile.is_nid) {
      total = (color === 'bw' ? RATES.bw_single : RATES.color_single) * copies;
      billSummaryText.innerText = `NID কার্ড (${colorBn}) × ${bn(copies)} সেট`;
    } else if (checked('paper_size') === '4x6' && uploadedFile.file_type !== 'pdf') {
      total = RATES.photo_4x6 * copies;
      billSummaryText.innerText = `৪×৬ পাসপোর্ট শিট × ${bn(copies)} কপি`;
    } else {
      const duplex = checked('duplex');
      const nup = uploadedFile.file_type === 'pdf' ? parseInt(checked('pages_per_sheet') || '1', 10) : 1;
      const sheets = Math.ceil(uploadedFile.page_count / nup);
      const rate = color === 'bw'
        ? (duplex === 'double' ? RATES.bw_double : RATES.bw_single)
        : (duplex === 'double' ? RATES.color_double : RATES.color_single);
      total = rate * sheets * copies;
      billSummaryText.innerText = `${bn(sheets)} টি শিট (${nup > 1 ? nup + '-in-1' : 'স্বাভাবিক'}, ${colorBn}) × ${bn(copies)} কপি`;
    }
    totalPriceText.innerText = `৳ ${total.toFixed(2)}`;
  }

  window.copyMfsNumber = function () {
    navigator.clipboard.writeText($('mfsNumber').innerText);
    alert('নম্বর কপি করা হয়েছে!');
  };


  // ── PHOTO STUDIO ─────────────────────────────────────────────────────────
  const PRESETS = [
    { id: 'p4555', name: 'পাসপোর্ট ৪৫×৫৫ মিমি', w: 45, h: 55 },
    { id: 'p3545', name: 'পাসপোর্ট/ভিসা ৩৫×৪৫ মিমি', w: 35, h: 45 },
    { id: 'p3851', name: 'স্টুডিও পাসপোর্ট ১.৫×২ ইঞ্চি (৩৮×৫১ মিমি)', w: 38, h: 51 },
    { id: 'p2530', name: 'ই-পাসপোর্ট অনলাইন ২৫×৩০ মিমি', w: 25, h: 30 },
    { id: 'stamp', name: 'স্ট্যাম্প সাইজ ২০×২৫ মিমি', w: 20, h: 25 },
    { id: 'custom', name: 'নিজের সাইজ (মিমি)', w: 40, h: 50 }
  ];
  let studioItems = [{ preset: 'p4555', w: 45, h: 55, count: 6, photo_idx: 0, zoom: 1.0, ox: 0.0, oy: 0.0 }];
  let studioPhotos = []; // [{ file_id, name, url }]
  let activeStudioRowIndex = 0;
  let studioBg = 'none';
  let studioSuit = 'none';
  let studioBlobUrl = null;
  let studioTimer = null;
  let studioInited = false;

  function syncStudioPhotosFromUploaded() {
    if (uploadedFile && uploadedFile.file_type === 'image') {
      const exists = studioPhotos.some(p => p.file_id === uploadedFile.file_id);
      if (!exists) {
        studioPhotos.unshift({
          file_id: uploadedFile.file_id,
          name: uploadedFile.original_filename || 'ছবি ১',
          url: instantLocalUrl || `/api/preview/${encodeURIComponent(uploadedFile.file_id)}?w=300`
        });
      }
    }
    renderStudioPhotoPool();
  }

  let activeStudioPhotoIndex = 0;

  window.selectStudioPhoto = function(idx) {
    if (!studioPhotos || idx < 0 || idx >= studioPhotos.length) return;
    activeStudioPhotoIndex = idx;
    const p = studioPhotos[idx];
    if (!p) return;

    // Set uploadedFile to this specific photo so rotate, crop, camscan, removeBg operate on it
    uploadedFile = {
      file_id: p.file_id,
      original_filename: p.name,
      file_type: 'image',
      page_count: 1,
      file_size: p.file_size || 0,
      is_office: false,
      doc_kind: null,
      office_orientation: 'portrait',
      preview_url: `/api/preview/${encodeURIComponent(p.file_id)}`
    };
    instantLocalUrl = p.url;

    // Sync activeStudioRowIndex if appropriate
    if (studioItems[activeStudioRowIndex]) {
      studioItems[activeStudioRowIndex].photo_idx = idx;
      studioItems[activeStudioRowIndex].file_id = p.file_id;
    }

    // Update fileNameText and badges at top
    const nameEl = $('fileNameText');
    if (nameEl) nameEl.innerText = p.name;
    const metaEl = $('fileMetaText');
    if (metaEl) metaEl.innerText = `📸 ছবি ${bn(idx + 1)} • ফটো পুল`;
    const iconEl = $('fileIcon');
    if (iconEl) iconEl.innerText = '🖼️';

    // Show fileInfoBadge and ensure toolbar buttons are visible for editing
    if (fileInfoBadge) fileInfoBadge.classList.remove('hidden');
    const btnCrop = $('btnOpenCrop');
    const btnCam = $('btnOpenCamScan');
    const btnRbg = $('btnOpenRemoveBg');
    const btnRot = $('btnRotateFile');
    const btnPrev = $('btnOpenPreview');

    if (btnCrop) btnCrop.classList.remove('hidden');
    if (btnCam) btnCam.classList.remove('hidden');
    if (btnRbg) btnRbg.classList.remove('hidden');
    if (btnRot) btnRot.classList.remove('hidden');
    if (btnPrev) btnPrev.classList.remove('hidden');

    // Force top live preview to reload this photo
    const img = $('docInlineImg');
    if (img) img.dataset.revKey = '';
    refreshTopLivePreview(true);

    // Re-render UI
    renderStudioPhotoPool();
    renderStudioItems();
    scheduleStudio();

    // Scroll top live preview container smoothly into view
    const topBadge = $('fileInfoBadge');
    if (topBadge) {
      topBadge.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  };

  function renderStudioPhotoPool() {
    const box = $('studioPhotoPool');
    if (!box) return;
    box.innerHTML = '';
    studioPhotos.forEach((p, idx) => {
      const isSelected = (uploadedFile && uploadedFile.file_id === p.file_id) || (idx === activeStudioPhotoIndex);
      const item = document.createElement('div');
      item.className = `relative flex items-center gap-2 p-1.5 rounded-xl cursor-pointer transition select-none ${
        isSelected
          ? 'bg-pink-100/90 border-2 border-pink-600 shadow-md ring-2 ring-pink-400/50 scale-[1.02]'
          : 'bg-white border border-pink-200 hover:border-pink-400 hover:bg-pink-50/60 shadow-xs'
      }`;
      item.title = 'ক্লিক করে এই ছবিটি প্রিভিউ ও এডিট করুন';
      item.onclick = (e) => {
        if (e.target.closest('button')) return;
        selectStudioPhoto(idx);
      };
      item.innerHTML = `
        <div class="relative shrink-0">
          <img src="${p.url}" alt="${p.name}" class="w-10 h-12 object-cover rounded-lg border ${isSelected ? 'border-pink-500 ring-1 ring-pink-400' : 'border-gray-200'}">
          <span class="absolute -top-1.5 -left-1.5 w-4 h-4 rounded-full ${isSelected ? 'bg-pink-600 text-white' : 'bg-gray-700 text-white'} text-[9px] font-bold flex items-center justify-center shadow">
            ${bn(idx + 1)}
          </span>
        </div>
        <div class="text-[10px] leading-tight pr-1 min-w-[70px]">
          <p class="font-bold flex items-center gap-1 ${isSelected ? 'text-pink-900' : 'text-gray-800'}">
            <span>ছবি ${bn(idx + 1)}</span>
            ${isSelected ? '<span class="text-[9px] bg-pink-600 text-white px-1.5 py-0.5 rounded-full font-bold shadow-xs">সিলেক্টেড</span>' : ''}
          </p>
          <p class="text-gray-500 truncate max-w-[90px] mt-0.5">${p.name}</p>
          <p class="text-[9px] ${isSelected ? 'text-pink-700 font-semibold' : 'text-gray-400'} mt-0.5">
            ${isSelected ? '👆 লাইভ প্রিভিউতে সক্রিয়' : 'ক্লিক করুন'}
          </p>
        </div>
        ${studioPhotos.length > 1 ? `
          <button type="button" onclick="event.stopPropagation(); removeStudioPhoto(${idx})" class="text-gray-400 hover:text-red-600 text-sm font-bold px-1.5 py-0.5 rounded-lg hover:bg-red-50 self-start ml-auto" title="ছবি মুছুন">&times;</button>
        ` : ''}
      `;
      box.appendChild(item);
    });
  }

  window.handleAddStudioPhoto = async function(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const btn = $('btnAddStudioPhoto');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="inline-block animate-spin mr-1">⏳</span> <span>আপলোড হচ্ছে...</span>';
    }
    try {
      let uploadTarget = file;
      if (typeof compressImageBeforeUpload === 'function') {
        try {
          uploadTarget = await compressImageBeforeUpload(file, 2000, 0.88);
        } catch (err) {
          console.warn('Image compression skipped:', err);
        }
      }
      const form = new FormData();
      form.append('file', uploadTarget, file.name);
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (res.ok && (data.file_id || data.success)) {
        let localUrl = '';
        try { localUrl = URL.createObjectURL(file); } catch (e) {}
        const newPhoto = {
          file_id: data.file_id,
          name: data.original_filename || file.name,
          url: localUrl || `/api/preview/${encodeURIComponent(data.file_id)}?w=300`
        };
        studioPhotos.push(newPhoto);
        selectStudioPhoto(studioPhotos.length - 1);
      } else {
        alert(data.detail || 'ছবি আপলোড ব্যর্থ হয়েছে');
      }
    } catch (e) {
      alert('ছবি আপলোড ব্যর্থ হয়েছে: ' + (e.message || e));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
      input.value = '';
    }
  };

  window.removeStudioPhoto = function(idx) {
    if (studioPhotos.length <= 1) {
      alert('কমপক্ষে একটি ছবি থাকতে হবে।');
      return;
    }
    studioPhotos.splice(idx, 1);
    studioItems.forEach(it => {
      if (it.photo_idx === idx) it.photo_idx = 0;
      else if (it.photo_idx > idx) it.photo_idx--;
    });
    if (activeStudioPhotoIndex >= studioPhotos.length) {
      activeStudioPhotoIndex = studioPhotos.length - 1;
    }
    selectStudioPhoto(activeStudioPhotoIndex);
  };

  function updateActiveRowUI() {
    if (activeStudioRowIndex >= studioItems.length) {
      activeStudioRowIndex = Math.max(0, studioItems.length - 1);
    }
    const it = studioItems[activeStudioRowIndex];
    if (!it) return;

    const label = $('studioActiveRowLabel');
    if (label) {
      const pName = (PRESETS.find(x => x.id === it.preset) || {}).name || `${it.w}×${it.h} মিমি`;
      label.innerText = `সারি ${bn(activeStudioRowIndex + 1)} (${pName})`;
    }

    const z = $('studioZoom');
    const ox = $('studioOx');
    const oy = $('studioOy');
    const valZ = $('valZoom');

    if (z) z.value = Math.round((it.zoom !== undefined ? it.zoom : 1.0) * 100);
    if (ox) ox.value = Math.round((it.ox !== undefined ? it.ox : 0.0) * 100);
    if (oy) oy.value = Math.round((it.oy !== undefined ? it.oy : 0.0) * 100);
    if (valZ) valZ.innerText = (it.zoom !== undefined ? it.zoom : 1.0).toFixed(1) + '×';
  }

  function renderStudioItems() {
    const box = $('studioItems');
    if (!box) return;
    box.innerHTML = '';
    studioItems.forEach((it, i) => {
      if (it.photo_idx === undefined) it.photo_idx = 0;
      if (it.zoom === undefined) it.zoom = 1.0;
      if (it.ox === undefined) it.ox = 0.0;
      if (it.oy === undefined) it.oy = 0.0;

      const isActive = activeStudioRowIndex === i;
      const row = document.createElement('div');
      row.className = `border-2 rounded-xl p-2.5 space-y-2 transition ${isActive ? 'border-pink-500 bg-pink-50/30 shadow-xs ring-1 ring-pink-400' : 'border-gray-200 bg-gray-50'}`;

      const photoOptions = studioPhotos.length > 1
        ? `<div class="flex items-center gap-1.5 text-xs bg-white p-1.5 rounded-lg border">
             <span class="text-gray-600 font-bold shrink-0">ছবি নির্বাচন:</span>
             <select data-i="${i}" data-k="photo_idx" class="flex-1 px-2 py-0.5 border border-pink-300 rounded text-xs bg-pink-50/50 font-bold text-gray-800">
               ${studioPhotos.map((p, pIdx) => `<option value="${pIdx}" ${pIdx === it.photo_idx ? 'selected' : ''}>📸 ছবি ${bn(pIdx + 1)} (${p.name.slice(0, 16)})</option>`).join('')}
             </select>
           </div>`
        : '';

      row.innerHTML = `
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-bold text-gray-800 flex items-center gap-1.5">
            <span class="w-5 h-5 rounded-full ${isActive ? 'bg-pink-600 text-white' : 'bg-gray-200 text-gray-700'} flex items-center justify-center text-[11px] font-bold">${bn(i + 1)}</span>
            <span>সারি ${bn(i + 1)}</span>
          </span>
          <button type="button" data-edit-row="${i}" class="px-2.5 py-0.5 rounded-lg text-xs font-bold transition flex items-center gap-1 ${isActive ? 'bg-pink-600 text-white shadow-xs' : 'bg-white border border-gray-300 hover:bg-pink-50 text-gray-700'}">
            <span>${isActive ? '✏️ এডিটিং সক্রিয়' : '🔍 জুম ও ক্রপ'}</span>
          </button>
          ${i > 0 ? `<button type="button" data-rm="${i}" class="text-gray-400 hover:text-red-600 font-bold px-1 text-sm" title="এই সারি মুছুন">&times;</button>` : ''}
        </div>

        <div class="flex gap-2">
          <select data-i="${i}" data-k="preset" class="flex-1 min-w-0 px-2 py-1.5 border rounded-lg text-xs bg-white font-medium">
            ${PRESETS.map((p) => `<option value="${p.id}" ${p.id === it.preset ? 'selected' : ''}>${p.name}</option>`).join('')}
          </select>
          <div class="flex items-center gap-1 bg-white border rounded-lg px-2 py-1">
            <span class="text-[11px] text-gray-500">কপি:</span>
            <input type="number" data-i="${i}" data-k="count" min="1" max="60" value="${it.count}" title="কয়টি" class="w-12 text-xs font-bold text-center focus:outline-none">
          </div>
        </div>

        ${photoOptions}

        ${it.preset === 'custom' ? `<div class="flex items-center gap-2 text-xs">
          চওড়া <input type="number" data-i="${i}" data-k="w" min="10" max="190" value="${it.w}" class="w-16 px-2 py-1 border rounded text-center font-bold"> মিমি ×
          উচ্চতা <input type="number" data-i="${i}" data-k="h" min="10" max="280" value="${it.h}" class="w-16 px-2 py-1 border rounded text-center font-bold"> মিমি</div>` : ''}
      `;
      box.appendChild(row);
    });

    if (studioItems.length < 12) {
      const add = document.createElement('button');
      add.type = 'button';
      add.dataset.add = '1';
      add.className = 'w-full py-2 border-2 border-dashed border-gray-300 rounded-xl text-xs font-bold text-gray-600 hover:border-pink-500 hover:text-pink-600 transition flex items-center justify-center gap-1.5 bg-white';
      add.innerText = '➕ আরো ছবির সাইজ ও সংখ্যা যোগ করুন (পাসপোর্ট / স্ট্যাম্প / কাস্টম)';
      box.appendChild(add);
    }

    updateActiveRowUI();
  }

  $('studioItems').addEventListener('input', (e) => {
    const i = e.target.dataset.i, k = e.target.dataset.k;
    if (i === undefined) return;
    const it = studioItems[+i];
    if (k === 'count') it.count = Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 1));
    else if (k === 'w' || k === 'h') it[k] = Math.max(10, parseFloat(e.target.value) || 10);
    scheduleStudio();
  });

  $('studioItems').addEventListener('change', (e) => {
    if (e.target.dataset.k === 'preset') {
      const it = studioItems[+e.target.dataset.i];
      const p = PRESETS.find((x) => x.id === e.target.value);
      it.preset = p.id; it.w = p.w; it.h = p.h;
      renderStudioItems();
      scheduleStudio();
    } else if (e.target.dataset.k === 'photo_idx') {
      const it = studioItems[+e.target.dataset.i];
      it.photo_idx = parseInt(e.target.value, 10) || 0;
      scheduleStudio();
    }
  });

  $('studioItems').addEventListener('click', (e) => {
    const editBtn = e.target.closest('[data-edit-row]');
    if (editBtn) {
      activeStudioRowIndex = +editBtn.dataset.editRow;
      renderStudioItems();
      return;
    }
    if (e.target.dataset.rm) {
      studioItems.splice(+e.target.dataset.rm, 1);
      if (activeStudioRowIndex >= studioItems.length) activeStudioRowIndex = Math.max(0, studioItems.length - 1);
      renderStudioItems();
      scheduleStudio();
    } else if (e.target.dataset.add) {
      studioItems.push({
        preset: 'stamp',
        w: 20,
        h: 25,
        count: 8,
        photo_idx: studioPhotos.length > 1 ? 1 : 0,
        zoom: 1.0,
        ox: 0.0,
        oy: 0.0
      });
      activeStudioRowIndex = studioItems.length - 1;
      renderStudioItems();
      scheduleStudio();
    }
  });

  function studioParams() {
    return {
      paper: $('studioPaper').value,
      photos: studioPhotos.map(p => p.file_id),
      items: studioItems.map((it) => ({
        w: it.w,
        h: it.h,
        count: it.count,
        file_id: (studioPhotos[it.photo_idx] || studioPhotos[0] || {}).file_id || (uploadedFile ? uploadedFile.file_id : ''),
        photo_idx: it.photo_idx || 0,
        zoom: it.zoom !== undefined ? it.zoom : 1.0,
        ox: it.ox !== undefined ? it.ox : 0.0,
        oy: it.oy !== undefined ? it.oy : 0.0
      })),
      fill: $('studioFill').checked,
      brightness: +$('studioBrightness').value,
      contrast: +$('studioContrast').value,
      bw: $('studioBw').value === '1',
      border: $('studioBorder').checked,
      bg: 'none',
      tol: 45,
      zoom: 1.0,
      ox: 0.0,
      oy: 0.0,
      suit: 'none',
      suit_zoom: 1,
      suit_ox: 0,
      suit_oy: 0
    };
  }

  function scheduleStudio() {
    $('valBrightness').innerText = $('studioBrightness').value;
    $('valContrast').innerText = $('studioContrast').value;
    const activeIt = studioItems[activeStudioRowIndex];
    if (activeIt) {
      $('valZoom').innerText = (activeIt.zoom !== undefined ? activeIt.zoom : 1.0).toFixed(1) + '×';
    }
    calculateBill();
    refreshTopLivePreview();
    clearTimeout(studioTimer);
    studioTimer = setTimeout(() => refreshStudio(false), 250);
  }

  let studioReq = 0;
  async function refreshStudio(force) {
    if (!uploadedFile || currentMode !== 'studio') return;
    const my = ++studioReq;
    try {
      const res = await fetch('/api/studio-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_id: (studioPhotos[0] || {}).file_id || uploadedFile.file_id,
          photos: studioPhotos.map(p => p.file_id),
          params: studioParams()
        })
      });
      if (!res.ok) throw new Error('preview failed');
      const info = JSON.parse(res.headers.get('X-Studio-Info') || '{}');
      const blob = await res.blob();
      if (my !== studioReq) return;
      if (studioBlobUrl) URL.revokeObjectURL(studioBlobUrl);
      studioBlobUrl = URL.createObjectURL(blob);
      $('studioPreviewImg').src = studioBlobUrl;
      const placed = (info.placed || []).map(bn).join(' + ');
      const miss = (info.requested || []).some((r, i) => r !== null && info.placed[i] < r);
      $('studioInfo').innerText = `মোট ${bn(info.total || 0)}টি ছবি বসেছে${placed ? ' (' + placed + ')' : ''}${miss ? ' ⚠️ সব ধরেনি' : ''}`;
      $('studioInfo').className = 'font-bold ' + (miss ? 'text-red-600' : 'text-pink-700');
    } catch (e) {
      $('studioInfo').innerText = '⚠️ প্রিভিউ তৈরি হয়নি';
    }
  }

  function initStudio() {
    if (!studioInited) {
      studioInited = true;
      syncStudioPhotosFromUploaded();
      renderStudioItems();
      ['studioBrightness', 'studioContrast'].forEach((id) => {
        const el = $(id);
        if (el) el.addEventListener('input', scheduleStudio);
      });
      ['studioZoom', 'studioOx', 'studioOy'].forEach((id) => {
        const el = $(id);
        if (el) {
          el.addEventListener('input', () => {
            const it = studioItems[activeStudioRowIndex];
            if (it) {
              if (id === 'studioZoom') it.zoom = +el.value / 100;
              else if (id === 'studioOx') it.ox = +el.value / 100;
              else if (id === 'studioOy') it.oy = +el.value / 100;
            }
            scheduleStudio();
          });
        }
      });
      ['studioPaper', 'studioBw', 'studioFill', 'studioBorder'].forEach((id) => {
        const el = $(id);
        if (el) el.addEventListener('change', scheduleStudio);
      });
      $('btnStudioMinus').addEventListener('click', () => { const v = +$('studioCopies').value; if (v > 1) { $('studioCopies').value = v - 1; scheduleStudio(); } });
      $('btnStudioPlus').addEventListener('click', () => { const v = +$('studioCopies').value; if (v < 100) { $('studioCopies').value = v + 1; scheduleStudio(); } });
    } else {
      syncStudioPhotosFromUploaded();
    }
    scheduleStudio();
  }

  // ── PRINT PREVIEW MODAL ──────────────────────────────────────────────────
  const overlay = $('previewModalOverlay');
  const pagesBox = $('previewPages');
  const loadingEl = $('previewLoading');
  const pdfFrame = $('previewPdfFrame');
  const noteEl = $('previewGuideNote');
  const MAX_PREVIEW_SHEETS = 12;

  function addPreviewImage(url, caption) {
    const fig = document.createElement('figure');
    fig.className = 'space-y-1';
    const img = document.createElement('img');
    img.className = 'w-full bg-white rounded shadow border';
    img.loading = 'lazy';
    img.alt = caption;
    img.onload = () => loadingEl.classList.add('hidden');
    img.onerror = () => {
      loadingEl.classList.add('hidden');
      img.replaceWith(Object.assign(document.createElement('p'), {
        className: 'text-center text-xs text-red-600 py-6',
        innerText: '⚠️ প্রিভিউ তৈরি করা যায়নি।'
      }));
    };
    img.src = url;
    const cap = document.createElement('figcaption');
    cap.className = 'text-center text-[11px] font-semibold text-gray-600';
    cap.innerText = caption;
    fig.append(img, cap);
    pagesBox.appendChild(fig);
  }

  async function openPrintPreview() {
    if (!uploadedFile) { alert('অনুগ্রহ করে আগে ফাইল সিলেক্ট বা আপলোড করুন।'); return; }

    pagesBox.innerHTML = '';
    pdfFrame.classList.add('hidden');
    pdfFrame.src = '';
    noteEl.innerText = '';
    loadingEl.classList.remove('hidden');
    overlay.classList.remove('hidden');

    $('previewModalFileName').innerText = uploadedFile.original_filename;
    $('previewModalTotal').innerText = totalPriceText.innerText;
    const color = checked('color_mode') || 'bw';
    const badge = $('previewModeBadge');
    badge.innerText = color === 'bw' ? 'সাদাকালো (B&W)' : 'রঙিন (Color)';
    badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold shrink-0 ' +
      (color === 'bw' ? 'bg-gray-200 text-gray-800' : 'bg-pink-100 text-pink-700');
    const dim = $('previewDimensionText');

    if (currentMode === 'studio') {
      const sp = studioParams();
      dim.innerText = sp.paper === '4x6' ? '৪×৬ ইঞ্চি ফটো পেপার' : 'A4 পেপার (আসল মাপে ছবি)';
      badge.innerText = sp.bw ? 'সাদাকালো (B&W)' : 'রঙিন (Color)';
      await refreshStudio(true);
      if (studioBlobUrl) addPreviewImage(studioBlobUrl, 'প্রিন্ট শিট');
      noteEl.innerText = '✂️ ছবির দাগ বরাবর কাটুন। প্রিন্টে "Actual size / 100%" দিন।';
      return;
    }

    if (uploadedFile.is_nid) {
      dim.innerText = 'NID কার্ড: ৮৫.৬ মিমি × ৫৪ মিমি (আসল মাপ)';
      // Build full URL to avoid cached errors
      const previewSrc = nidPreviewUrl(900);
      addPreviewImage(previewSrc, 'A4 পেজ — সামনে ও পেছনে (আসল মাপ)');
      noteEl.innerText = '✂️ ড্যাশ লাইন বরাবর কাটুন। প্রিন্টে "Actual size / 100%" দিন; নিচের ৫০ মিমি দাগ মেপে সাইজ মিলিয়ে নিন।';
      return;
    }

    const orient = checked('orientation') || 'portrait';
    const orientBn = orient === 'landscape' ? 'আড়াআড়ি (Landscape)' : 'লম্বালম্বি (Portrait)';
    const btnOrientIcon = $('previewOrientIcon');
    const btnOrientText = $('previewOrientText');
    if (btnOrientIcon && btnOrientText) {
      if (orient === 'landscape') {
        btnOrientIcon.innerText = '📱';
        btnOrientText.innerText = 'Portrait করুন';
      } else {
        btnOrientIcon.innerText = '🖥️';
        btnOrientText.innerText = 'Landscape করুন';
      }
    }

    if (uploadedFile.file_type === 'image') {
      const photo = checked('paper_size') === '4x6';
      dim.innerText = photo ? '৪×৬ ইঞ্চি পাসপোর্ট শিট (৬টি ফটো)' : `A4 পেপার — ${orientBn}`;
      addPreviewImage(
        `/api/preview-page/${uploadedFile.file_id}?paper_size=${photo ? '4x6' : 'A4'}&color=${color}&orientation=${orient}&t=${Date.now()}`,
        photo ? '৪×৬ ফটো শিট' : `A4 পেজ (${orientBn})`
      );
      noteEl.innerText = photo ? '✂️ ৬টি ছবি ৪×৬ ফটো পেপারে সাজানো হবে।' : '📄 ছবি A4 পাতায় ফিট করে প্রিন্ট হবে।';
      return;
    }

    // PDF
    const nup = parseInt(checked('pages_per_sheet') || '1', 10);
    dim.innerText = nup > 1
      ? `A4 পেপার (${nup}-in-1, ${bn(uploadedFile.page_count)} পাতা, ${orientBn})`
      : `A4 পেপার (${bn(uploadedFile.page_count)} টি পাতা, ${orientBn})`;
    try {
      const info = await (await fetch(`/api/preview-info/${uploadedFile.file_id}?nup=${nup}`)).json();
      if (!info.can_render) throw new Error('no renderer');
      const shown = Math.min(info.sheets, MAX_PREVIEW_SHEETS);
      for (let i = 1; i <= shown; i++) {
        addPreviewImage(
          `/api/preview-page/${uploadedFile.file_id}?page=${i}&nup=${nup}&color=${color}&orientation=${orient}&t=${Date.now()}`,
          `শিট ${bn(i)} / ${bn(info.sheets)}`
        );
      }
      if (info.sheets > shown) {
        noteEl.innerText = `প্রথম ${bn(shown)} টি শিট দেখানো হয়েছে (মোট ${bn(info.sheets)} টি)।`;
        const btnLoadAll = document.createElement('button');
        btnLoadAll.type = 'button';
        btnLoadAll.className = 'w-full py-2.5 bg-pink-600 hover:bg-pink-700 text-white text-xs font-bold rounded-xl shadow transition';
        btnLoadAll.innerText = `📄 বাকি সবগুলো পাতা দেখুন (${bn(shown + 1)} থেকে ${bn(info.sheets)} পর্যন্ত)`;
        btnLoadAll.addEventListener('click', () => {
          btnLoadAll.remove();
          for (let j = shown + 1; j <= info.sheets; j++) {
            addPreviewImage(
              `/api/preview-page/${uploadedFile.file_id}?page=${j}&nup=${nup}&color=${color}&orientation=${orient}&t=${Date.now()}`,
              `শিট ${bn(j)} / ${bn(info.sheets)}`
            );
          }
          noteEl.innerText = `সবগুলো (${bn(info.sheets)} টি) শিট দেখানো হচ্ছে।`;
        });
        pagesBox.appendChild(btnLoadAll);
      }
    } catch (err) {
      // fallback: browser's own PDF viewer
      loadingEl.classList.add('hidden');
      pdfFrame.classList.remove('hidden');
      pdfFrame.src = (uploadedFile.preview_url || `/api/preview/${uploadedFile.file_id}`) + '#toolbar=0';
      noteEl.innerText = '📄 ডকুমেন্টের পাতাগুলো স্ক্রল করে দেখুন';
    }
  }

  function closePrintPreview() {
    overlay.classList.add('hidden');
    pdfFrame.src = '';
    pagesBox.innerHTML = '';
  }

  ['btnOpenPreview', 'btnOpenPreviewMini', 'btnOpenPreviewBanner'].forEach((id) => {
    const b = $(id);
    if (b) b.addEventListener('click', openPrintPreview);
  });
  $('btnClosePreviewModal').addEventListener('click', closePrintPreview);
  $('btnClosePreviewBtn').addEventListener('click', closePrintPreview);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closePrintPreview(); });
  $('btnConfirmFromPreview').addEventListener('click', () => {
    closePrintPreview();
    paymentCard.scrollIntoView({ behavior: 'smooth' });
    const btn = $('btnConfirmOrder');
    btn.classList.add('ring-4', 'ring-pink-300');
    setTimeout(() => btn.classList.remove('ring-4', 'ring-pink-300'), 1500);
  });

  // ── RECENT ORDERS & LIVE TRACKER ─────────────────────────────────────────
  let activeTrackerOrderId = null;
  const orderSockets = {};

  function loadRecentOrders() {
    try {
      const raw = localStorage.getItem(RECENT_ORDERS_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }

  function saveRecentOrders(list) {
    try {
      localStorage.setItem(RECENT_ORDERS_KEY, JSON.stringify((list || []).slice(0, 15)));
    } catch (e) {}
  }

  function recordRecentOrder(orderObj) {
    const list = loadRecentOrders().filter((x) => x && x.id !== orderObj.id);
    list.unshift(orderObj);
    saveRecentOrders(list);
    renderRecentOrdersPanel();
    ensureOrderSocket(orderObj.id);
  }

  function updateRecentOrderStatus(orderId, status, message) {
    const list = loadRecentOrders();
    let changed = false;
    list.forEach((item) => {
      if (item && item.id === orderId) {
        if (status && item.status !== status) {
          item.status = status;
          changed = true;
        }
        if (message) {
          item.message = message;
          changed = true;
        }
      }
    });
    if (changed) {
      saveRecentOrders(list);
    }
    renderRecentOrdersPanel();
    if (activeTrackerOrderId === orderId) {
      applyStatusToTrackerModal(status, message);
    }
  }

  function getStatusMeta(status) {
    if (status === 'paid') {
      return {
        badge: '💳 পেইড / প্রিন্টের অপেক্ষায়',
        cls: 'bg-blue-100 text-blue-800 border-blue-200',
        defaultMsg: 'পেমেন্ট গৃহীত হয়েছে। দোকানদার প্রিন্ট শুরু করছেন...'
      };
    }
    if (status === 'printing') {
      return {
        badge: '⚡ প্রিন্ট হচ্ছে...',
        cls: 'bg-pink-100 text-pink-700 border-pink-200 animate-pulse',
        defaultMsg: 'প্রিন্টারে আপনার ফাইলটি প্রিন্ট হচ্ছে...'
      };
    }
    if (status === 'completed') {
      return {
        badge: '✓ প্রিন্ট সম্পন্ন',
        cls: 'bg-green-100 text-green-800 border-green-200',
        defaultMsg: 'আপনার প্রিন্ট সফলভাবে সম্পন্ন হয়েছে! কাউন্টার থেকে কাগজ সংগ্রহ করুন।'
      };
    }
    if (status === 'cancelled') {
      return {
        badge: '❌ বাতিল',
        cls: 'bg-red-100 text-red-700 border-red-200',
        defaultMsg: 'এই প্রিন্ট অর্ডারটি বাতিল করা হয়েছে।'
      };
    }
    return {
      badge: '⏳ পেমেন্ট অপেক্ষমাণ',
      cls: 'bg-amber-100 text-amber-800 border-amber-200',
      defaultMsg: 'কাউন্টার থেকে পেমেন্ট ও অর্ডার নিশ্চিত করা হচ্ছে। অনুগ্রহ করে অপেক্ষা করুন...'
    };
  }

  function renderRecentOrdersPanel() {
    const list = loadRecentOrders();
    const badge = $('recentPrintsBadge');
    const box = $('recentPrintsList');
    if (badge) {
      if (list.length > 0) {
        badge.innerText = bn(list.length);
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
    }
    if (!box) return;
    if (list.length === 0) {
      box.innerHTML = '<p class="text-xs text-gray-400 text-center py-4">এখনো কোনো প্রিন্ট অর্ডার পাঠানো হয়নি।</p>';
      return;
    }
    box.innerHTML = '';
    list.forEach((item) => {
      const meta = getStatusMeta(item.status);
      const row = document.createElement('div');
      row.className = 'p-2.5 rounded-xl bg-gray-50 hover:bg-pink-50/40 border border-gray-200/80 flex items-center justify-between gap-2 cursor-pointer transition';
      row.innerHTML = `
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5">
            <span class="font-bold text-xs text-gray-800 truncate">${item.original_filename || 'ডকুমেন্ট'}</span>
            <span class="text-[10px] font-mono text-gray-400 shrink-0">#${item.id}</span>
          </div>
          <div class="text-[11px] text-gray-500 mt-0.5 flex items-center gap-2 flex-wrap">
            <span>${bn(item.copies || 1)} কপি</span>
            <span>•</span>
            <span class="font-bold text-pink-600">৳ ${Number(item.total_price || 0).toFixed(0)}</span>
            ${item.created_at ? `<span>• ${item.created_at}</span>` : ''}
          </div>
        </div>
        <div class="flex flex-col items-end gap-1 shrink-0">
          <span class="px-2 py-0.5 rounded-full text-[10px] font-bold border ${meta.cls}">${meta.badge}</span>
          <span class="text-[10px] text-pink-600 font-bold underline">বিস্তারিত দেখুন ➔</span>
        </div>
      `;
      row.addEventListener('click', () => {
        openTracker(item.id);
      });
      box.appendChild(row);
    });
  }

  async function syncRecentOrdersStatus() {
    const list = loadRecentOrders();
    if (!list.length) {
      renderRecentOrdersPanel();
      return;
    }
    const ids = list.map((x) => x.id).filter(Boolean).join(',');
    try {
      const res = await fetch(`/api/orders/status?ids=${encodeURIComponent(ids)}&t=${Date.now()}`);
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.orders)) {
        const map = {};
        data.orders.forEach((o) => { map[o.id] = o; });
        list.forEach((item) => {
          const srv = map[item.id];
          if (srv) {
            item.status = srv.status;
            item.original_filename = srv.original_filename || item.original_filename;
            item.copies = srv.copies || item.copies;
            item.total_price = srv.total_price !== undefined ? srv.total_price : item.total_price;
          }
        });
        saveRecentOrders(list);
        renderRecentOrdersPanel();
        if (activeTrackerOrderId && map[activeTrackerOrderId]) {
          applyStatusToTrackerModal(map[activeTrackerOrderId].status, null);
        }
      }
    } catch (e) {}
    // Ensure active orders have live WebSockets connected
    list.forEach((item) => {
      if (item && item.id && item.status !== 'completed' && item.status !== 'cancelled') {
        ensureOrderSocket(item.id);
      }
    });
  }

  function ensureOrderSocket(orderId) {
    if (!orderId || orderSockets[orderId]) return;
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const sock = new WebSocket(`${proto}//${location.host}/ws/order/${orderId}`);
      orderSockets[orderId] = sock;
      sock.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.event === 'STATUS_CHANGE') {
            updateRecentOrderStatus(orderId, msg.status, msg.message);
          }
        } catch (e) {}
      };
      sock.onclose = () => {
        delete orderSockets[orderId];
      };
    } catch (e) {}
  }

  if ($('btnToggleRecentPrints')) {
    $('btnToggleRecentPrints').addEventListener('click', () => {
      const panel = $('recentPrintsPanel');
      if (!panel) return;
      const isHidden = panel.classList.contains('hidden');
      panel.classList.toggle('hidden', !isHidden);
      if (isHidden) {
        syncRecentOrdersStatus();
      }
    });
  }
  if ($('btnCloseRecentPrints')) {
    $('btnCloseRecentPrints').addEventListener('click', () => {
      if ($('recentPrintsPanel')) $('recentPrintsPanel').classList.add('hidden');
    });
  }
  if ($('btnRefreshRecentPrints')) {
    $('btnRefreshRecentPrints').addEventListener('click', () => {
      syncRecentOrdersStatus();
    });
  }

  // ── PLACE ORDER ──────────────────────────────────────────────────────────
  $('btnConfirmOrder').addEventListener('click', async () => {
    if (!uploadedFile) { alert('অনুগ্রহ করে ফাইল বা ছবি আপলোড করুন।'); return; }
    const paymentMethod = checked('payment_method');
    const trxId = $('trxIdInput').value.trim();
    if ((paymentMethod === 'bkash' || paymentMethod === 'nagad') && !trxId) {
      alert('অনুগ্রহ করে বিকাশের/নগদের TrxID লিখুন।');
      return;
    }

    const isNid = !!uploadedFile.is_nid;
    const btn = $('btnConfirmOrder');
    const origBtnHtml = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('opacity-80', 'cursor-wait');
    btn.innerHTML = '<span class="inline-block animate-spin mr-1.5">⏳</span> <span>প্রিন্টের জন্য পাঠানো হচ্ছে... অনুগ্রহ করে অপেক্ষা করুন</span>';

    const payload = {
      shop_id: SHOP_ID,
      file_id: uploadedFile.file_id,
      original_filename: uploadedFile.original_filename,
      copies: parseInt((currentMode === 'studio' ? $('studioCopies') : inputCopies).value, 10),
      paper_size: isNid ? 'NID' : (currentMode === 'studio' ? 'PHOTO' : 'A4'),
      studio: currentMode === 'studio' ? studioParams() : undefined,
      color_mode: currentMode === 'studio' ? (studioParams().bw ? 'bw' : 'color') : checked('color_mode'),
      duplex: isNid ? 'single' : checked('duplex'),
      pages_per_sheet: (!isNid && uploadedFile.file_type === 'pdf') ? parseInt(checked('pages_per_sheet') || '1', 10) : 1,
      nid_fit: checked('nid_fit') || 'cover',
      nid_rot_front: nidRot.front,
      nid_rot_back: nidRot.back,
      orientation: checked('orientation') || 'portrait',
      image_pos: (uploadedFile && uploadedFile.file_type === 'image' && currentMode !== 'studio') ? {
        pos_x: imagePositionState.pos_x,
        pos_y: imagePositionState.pos_y,
        scale: imagePositionState.scale,
        fit_mode: imagePositionState.fit_mode
      } : undefined,
      printer_target: (currentMode === 'studio'
        ? ($('studioPrinterSelect') ? $('studioPrinterSelect').value : '')
        : ($('printerSelect') ? $('printerSelect').value : '')) || '',
      payment_method: paymentMethod,
      trx_id: trxId,
      customer_name: $('custNameInput').value.trim() || 'Walk-in'
    };

    try {
      const res = await fetch('/api/order/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) {
        recordRecentOrder({
          id: data.order_id,
          original_filename: uploadedFile.original_filename,
          copies: payload.copies,
          total_price: data.total_price,
          status: 'pending_payment',
          created_at: new Date().toLocaleTimeString('bn-BD', { hour: '2-digit', minute: '2-digit' })
        });
        openTracker(data.order_id);
      } else {
        alert(data.detail || 'অর্ডার তৈরিতে সমস্যা হয়েছে।');
      }
    } catch (err) {
      alert('সার্ভারে সংযোগ পাওয়া যায়নি।');
    } finally {
      btn.disabled = false;
      btn.classList.remove('opacity-80', 'cursor-wait');
      btn.innerHTML = origBtnHtml;
    }
  });

  const DOT_DONE = 'w-4 h-4 rounded-full bg-green-500 text-white flex items-center justify-center text-[10px]';
  const DOT_IDLE = 'w-4 h-4 rounded-full bg-gray-300 text-white flex items-center justify-center text-[10px]';

  function resetTrackerDots() {
    if ($('step2Dot')) { $('step2Dot').className = DOT_IDLE; $('step2Dot').innerText = '2'; }
    if ($('step3Dot')) { $('step3Dot').className = DOT_IDLE; $('step3Dot').innerText = '3'; }
    if ($('step4Dot')) { $('step4Dot').className = DOT_IDLE; $('step4Dot').innerText = '4'; }
    if ($('statusIcon')) $('statusIcon').innerText = '⏳';
    if ($('statusTitle')) $('statusTitle').innerText = 'অর্ডার গ্রহণ করা হয়েছে';
    if ($('statusMessage')) $('statusMessage').innerText = 'কাউন্টার থেকে পেমেন্ট নিশ্চিত করা হচ্ছে। অনুগ্রহ করে অপেক্ষা করুন...';
  }

  function applyStatusToTrackerModal(status, message) {
    resetTrackerDots();
    const meta = getStatusMeta(status);
    if ($('statusMessage')) $('statusMessage').innerText = message || meta.defaultMsg;
    if (status === 'paid') {
      $('step2Dot').className = DOT_DONE; $('step2Dot').innerText = '✓';
      $('statusIcon').innerText = '🖨️'; $('statusTitle').innerText = 'পেমেন্ট সম্পন্ন';
    } else if (status === 'printing') {
      $('step2Dot').className = DOT_DONE; $('step2Dot').innerText = '✓';
      $('step3Dot').className = 'w-4 h-4 rounded-full bg-pink-600 text-white flex items-center justify-center text-[10px] pulse-dot';
      $('statusIcon').innerText = '⚡'; $('statusTitle').innerText = 'প্রিন্ট হচ্ছে...';
    } else if (status === 'completed') {
      ['step2Dot', 'step3Dot', 'step4Dot'].forEach((d) => { $(d).className = DOT_DONE; $(d).innerText = '✓'; });
      $('statusIcon').innerText = '🎉'; $('statusTitle').innerText = 'প্রিন্ট সম্পন্ন!';
    } else if (status === 'cancelled') {
      $('statusIcon').innerText = '❌'; $('statusTitle').innerText = 'অর্ডার বাতিল';
    }
  }

  function openTracker(orderId) {
    activeTrackerOrderId = orderId;
    $('trackerOverlay').classList.remove('hidden');
    $('statusOrderId').innerText = `Order ID: #${orderId}`;
    const existing = loadRecentOrders().find((x) => x && x.id === orderId);
    applyStatusToTrackerModal(existing ? existing.status : 'pending_payment', existing ? existing.message : null);
    ensureOrderSocket(orderId);
    syncRecentOrdersStatus();
  }

  function closeTrackerOverlay() {
    $('trackerOverlay').classList.add('hidden');
    if ($('recentPrintsPanel')) {
      $('recentPrintsPanel').classList.remove('hidden');
    }
    renderRecentOrdersPanel();
  }

  function startNewPrintFromTracker() {
    $('trackerOverlay').classList.add('hidden');
    resetUploads(true);
    (currentMode === 'nid' ? dropzoneNid : dropzoneNormal).classList.remove('hidden');
    if ($('recentPrintsPanel')) {
      $('recentPrintsPanel').classList.remove('hidden');
    }
    renderRecentOrdersPanel();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if ($('btnCloseTrackerTop')) $('btnCloseTrackerTop').addEventListener('click', closeTrackerOverlay);
  if ($('btnTrackerGoBack')) $('btnTrackerGoBack').addEventListener('click', closeTrackerOverlay);
  if ($('btnTrackerNewPrint')) $('btnTrackerNewPrint').addEventListener('click', startNewPrintFromTracker);

  // ── FILE TRANSFER (customer -> shop) ─────────────────────────────────────
  let trFile = null;
  $('trDropzone').addEventListener('click', () => $('trFileInput').click());
  $('trFileInput').addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (f.size > MAX_BYTES) { alert('ফাইলের সাইজ সর্বোচ্চ ৫০ MB হতে পারবে।'); e.target.value = ''; return; }
    trFile = f;
    $('trFileLabel').innerText = `✓ ${f.name} (${fmtSize(f.size)})`;
    $('trSuccess').classList.add('hidden');
  });

  $('btnTrSend').addEventListener('click', async () => {
    if (!trFile) { alert('আগে একটি ফাইল বাছাই করুন।'); return; }
    const btn = $('btnTrSend');
    btn.disabled = true;
    $('trProgress').classList.remove('hidden');
    setBar($('trBar'), $('trPercent'), 0);

    const fd = new FormData();
    fd.append('file', trFile);
    fd.append('sender_name', $('trNameInput').value.trim());
    fd.append('note', $('trNoteInput').value.trim());
    const res = await sendForm(`/api/transfer/send/${encodeURIComponent(SHOP_CODE)}`, fd, (p) => setBar($('trBar'), $('trPercent'), p));
    $('trProgress').classList.add('hidden');
    btn.disabled = false;

    if (res.ok) {
      $('trSuccessId').innerText = res.json.transfer_id;
      $('trSuccess').classList.remove('hidden');
      trFile = null;
      $('trFileInput').value = '';
      $('trFileLabel').innerText = 'আরেকটি ফাইল বাছাই করতে ক্লিক করুন';
      $('trNoteInput').value = '';
    } else {
      alert(res.json.detail || 'ফাইল পাঠানো যায়নি। আবার চেষ্টা করুন।');
    }
  });

  function goToTransfer() {
    const code = $('trCodeInput').value.trim().toUpperCase();
    if (code.length < 4) { alert('দোকানদারের দেওয়া কোডটি লিখুন।'); return; }
    location.href = '/t/' + encodeURIComponent(code);
  }
  $('btnTrGet').addEventListener('click', goToTransfer);
  $('trCodeInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') goToTransfer(); });

  function joinDeviceRoomDirect() {
    const inp = $('roomJoinCodeDirect');
    if (!inp) return;
    const raw = inp.value.trim();
    const match = raw.match(/\/r\/([A-Za-z0-9]{4,12})/i);
    const code = (match ? match[1] : raw).toUpperCase();
    if (code.length < 4) { alert('অন্য ডিভাইসের রুম কোডটি লিখুন।'); return; }
    location.href = '/r/' + encodeURIComponent(code);
  }
  if ($('btnRoomJoinDirect')) $('btnRoomJoinDirect').addEventListener('click', joinDeviceRoomDirect);
  if ($('roomJoinCodeDirect')) $('roomJoinCodeDirect').addEventListener('keydown', (e) => { if (e.key === 'Enter') joinDeviceRoomDirect(); });

  // open the transfer tab directly via /p/shop#transfer
  if (location.hash === '#transfer') setMode('transfer');

  // ── Smart 4-Corner Perspective Crop (CamScanner Style) + Normal Cropper.js ──
  let cropper = null;
  let cropMode = 'smart'; // 'smart' | 'normal'
  let activeRatio = 'free';
  let smartTargetRatio = 'auto';
  let cropNidSide = 'front';

  let smartImg = new Image();
  let smartPts = [
    { x: 0.07, y: 0.07 },
    { x: 0.93, y: 0.07 },
    { x: 0.93, y: 0.93 },
    { x: 0.07, y: 0.93 }
  ];
  let smartGeom = { pad: 16, drawW: 300, drawH: 300 };
  let activeDragIdx = -1; // 0..3 = corners, 4..7 = edge midpoints

  function resetSmartPtsDefault() {
    smartPts = [
      { x: 0.07, y: 0.07 },
      { x: 0.93, y: 0.07 },
      { x: 0.93, y: 0.93 },
      { x: 0.07, y: 0.93 }
    ];
  }

  function detectCornersClientSide(imgEl) {
    try {
      const iw = imgEl.naturalWidth || imgEl.width;
      const ih = imgEl.naturalHeight || imgEl.height;
      if (!iw || !ih) return null;
      const maxD = 180;
      const sc = Math.min(1, maxD / Math.max(iw, ih));
      const sw = Math.max(32, Math.round(iw * sc));
      const sh = Math.max(32, Math.round(ih * sc));

      const cvs = document.createElement('canvas');
      cvs.width = sw;
      cvs.height = sh;
      const ctx = cvs.getContext('2d');
      ctx.drawImage(imgEl, 0, 0, sw, sh);
      const d = ctx.getImageData(0, 0, sw, sh).data;

      const t = Math.max(2, Math.round(0.04 * Math.min(sw, sh)));
      let sumR = 0, sumG = 0, sumB = 0, cnt = 0;
      for (let y = 0; y < sh; y++) {
        for (let x = 0; x < sw; x++) {
          if (y < t || y >= sh - t || x < t || x >= sw - t) {
            const idx = (y * sw + x) << 2;
            sumR += d[idx]; sumG += d[idx + 1]; sumB += d[idx + 2];
            cnt++;
          }
        }
      }
      const meanR = sumR / cnt, meanG = sumG / cnt, meanB = sumB / cnt;
      let varR = 0, varG = 0, varB = 0;
      for (let y = 0; y < sh; y++) {
        for (let x = 0; x < sw; x++) {
          if (y < t || y >= sh - t || x < t || x >= sw - t) {
            const idx = (y * sw + x) << 2;
            varR += (d[idx] - meanR) ** 2;
            varG += (d[idx + 1] - meanG) ** 2;
            varB += (d[idx + 2] - meanB) ** 2;
          }
        }
      }
      const stdR = Math.sqrt(varR / cnt) + 12.0;
      const stdG = Math.sqrt(varG / cnt) + 12.0;
      const stdB = Math.sqrt(varB / cnt) + 12.0;

      const rawMask = new Uint8Array(sw * sh);
      for (let i = 0, p = 0; i < d.length; i += 4, p++) {
        const dr = (d[i] - meanR) / stdR;
        const dg = (d[i + 1] - meanG) / stdG;
        const db = (d[i + 2] - meanB) / stdB;
        if (dr * dr + dg * dg + db * db > 3.42) rawMask[p] = 1;
      }

      // 3x3 morphological opening/density filter to strip stray dust
      const mask = new Uint8Array(sw * sh);
      let fgCount = 0;
      for (let y = 1; y < sh - 1; y++) {
        const row = y * sw;
        for (let x = 1; x < sw - 1; x++) {
          const p = row + x;
          if (!rawMask[p]) continue;
          const n = rawMask[p - sw - 1] + rawMask[p - sw] + rawMask[p - sw + 1] +
                    rawMask[p - 1] + rawMask[p] + rawMask[p + 1] +
                    rawMask[p + sw - 1] + rawMask[p + sw] + rawMask[p + sw + 1];
          if (n >= 6) {
            mask[p] = 1;
            fgCount++;
          }
        }
      }

      const ratio = fgCount / (sw * sh);
      if (ratio < 0.07 || ratio > 0.94) return null;

      let minS = 1e9, maxS = -1e9, maxDVal = -1e9, minDVal = 1e9;
      let tl = { x: 0.07, y: 0.07 }, tr = { x: 0.93, y: 0.07 }, br = { x: 0.93, y: 0.93 }, bl = { x: 0.07, y: 0.93 };
      for (let y = 1; y < sh - 1; y++) {
        const ny = y / (sh - 1);
        const row = y * sw;
        for (let x = 1; x < sw - 1; x++) {
          if (!mask[row + x]) continue;
          const nx = x / (sw - 1);
          const s = nx + ny;
          const df = nx - ny;
          if (s < minS) { minS = s; tl = { x: nx, y: ny }; }
          if (s > maxS) { maxS = s; br = { x: nx, y: ny }; }
          if (df > maxDVal) { maxDVal = df; tr = { x: nx, y: ny }; }
          if (df < minDVal) { minDVal = df; bl = { x: nx, y: ny }; }
        }
      }
      return [tl, tr, br, bl];
    } catch (e) {
      return null;
    }
  }

  let modalPdfPage = 1;
  let camscanApplyAllPages = false;

  window.autoDetectSmartCorners = async function(silentIfDragging) {
    if (!uploadedFile) return;
    if (smartImg && smartImg.complete && (smartImg.naturalWidth || smartImg.width) > 0) {
      const fastPts = detectCornersClientSide(smartImg);
      if (fastPts && (!silentIfDragging || activeDragIdx === -1)) {
        smartPts = fastPts;
      }
      drawSmartCrop();
    }
    try {
      const res = await fetch(`/api/image/detect-corners?file_id=${encodeURIComponent(uploadedFile.file_id)}&side=${encodeURIComponent(cropNidSide)}&page=${modalPdfPage}&t=${Date.now()}`);
      const data = await res.json();
      if (data.success && Array.isArray(data.points) && data.points.length === 4) {
        if (!silentIfDragging || activeDragIdx === -1) {
          smartPts = data.points.map(p => ({ x: +p.x, y: +p.y }));
          drawSmartCrop();
        }
      }
    } catch (e) {
      drawSmartCrop();
    }
  };

  function bilerp(p0, p1, p2, p3, u, v) {
    const tx = p0.x * (1 - u) + p1.x * u;
    const ty = p0.y * (1 - u) + p1.y * u;
    const bx = p3.x * (1 - u) + p2.x * u;
    const by = p3.y * (1 - u) + p2.y * u;
    return { x: tx * (1 - v) + bx * v, y: ty * (1 - v) + by * v };
  }

  function drawSmartCrop() {
    const canvas = $('smartCropCanvas');
    const cont = $('smartCropContainer');
    if (!canvas || !cont || !smartImg.width) return;

    const pad = 16;
    const availW = Math.max(200, (cont.clientWidth || 360) - pad * 2 - 4);
    const availH = Math.max(200, (cont.clientHeight || 340) - pad * 2 - 4);
    const scale = Math.min(availW / smartImg.width, availH / smartImg.height);
    const drawW = Math.round(smartImg.width * scale);
    const drawH = Math.round(smartImg.height * scale);

    canvas.width = drawW + pad * 2;
    canvas.height = drawH + pad * 2;
    smartGeom = { pad, drawW, drawH };

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // 1. Draw image
    ctx.drawImage(smartImg, pad, pad, drawW, drawH);

    const cPts = smartPts.map(p => ({
      x: pad + p.x * drawW,
      y: pad + p.y * drawH
    }));

    // 2. Darken outside polygon
    ctx.save();
    ctx.fillStyle = 'rgba(0, 0, 0, 0.58)';
    ctx.beginPath();
    ctx.rect(pad, pad, drawW, drawH);
    ctx.moveTo(cPts[0].x, cPts[0].y);
    ctx.lineTo(cPts[1].x, cPts[1].y);
    ctx.lineTo(cPts[2].x, cPts[2].y);
    ctx.lineTo(cPts[3].x, cPts[3].y);
    ctx.closePath();
    ctx.fill('evenodd');
    ctx.restore();

    // 3. Perspective 3x3 internal guide lines
    ctx.save();
    ctx.strokeStyle = 'rgba(16, 185, 129, 0.45)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    [1 / 3, 2 / 3].forEach(t => {
      const vTop = bilerp(cPts[0], cPts[1], cPts[2], cPts[3], t, 0);
      const vBot = bilerp(cPts[0], cPts[1], cPts[2], cPts[3], t, 1);
      ctx.beginPath(); ctx.moveTo(vTop.x, vTop.y); ctx.lineTo(vBot.x, vBot.y); ctx.stroke();

      const hLeft = bilerp(cPts[0], cPts[1], cPts[2], cPts[3], 0, t);
      const hRight = bilerp(cPts[0], cPts[1], cPts[2], cPts[3], 1, t);
      ctx.beginPath(); ctx.moveTo(hLeft.x, hLeft.y); ctx.lineTo(hRight.x, hRight.y); ctx.stroke();
    });
    ctx.restore();

    // 4. Main Quadrilateral Border
    ctx.save();
    ctx.strokeStyle = '#10b981';
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(cPts[0].x, cPts[0].y);
    ctx.lineTo(cPts[1].x, cPts[1].y);
    ctx.lineTo(cPts[2].x, cPts[2].y);
    ctx.lineTo(cPts[3].x, cPts[3].y);
    ctx.closePath();
    ctx.stroke();
    ctx.restore();

    // 5. Edge midpoint handles
    for (let i = 0; i < 4; i++) {
      const a = cPts[i];
      const b = cPts[(i + 1) % 4];
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      ctx.beginPath();
      ctx.arc(mx, my, 6.5, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#059669';
      ctx.stroke();
    }

    // 6. 4 Corner handles (large touch-friendly circles)
    cPts.forEach((pt, idx) => {
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 11, 0, Math.PI * 2);
      ctx.fillStyle = idx === activeDragIdx ? '#ec4899' : '#10b981';
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = '#ffffff';
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();
    });
  }

  function updateLoupe(normPt) {
    const loupe = $('smartLoupeCanvas');
    if (!loupe || !smartImg.width) return;
    loupe.classList.remove('hidden');
    // Move loupe to top-right if finger is in top-left
    if (normPt.x < 0.42 && normPt.y < 0.42) {
      loupe.style.left = 'auto';
      loupe.style.right = '10px';
    } else {
      loupe.style.right = 'auto';
      loupe.style.left = '10px';
    }

    const lctx = loupe.getContext('2d');
    const lw = loupe.width, lh = loupe.height;
    lctx.clearRect(0, 0, lw, lh);
    lctx.fillStyle = '#0f172a';
    lctx.fillRect(0, 0, lw, lh);

    const srcX = normPt.x * smartImg.width;
    const srcY = normPt.y * smartImg.height;
    const zoomRadius = Math.max(24, Math.min(smartImg.width, smartImg.height) * 0.08);

    lctx.drawImage(
      smartImg,
      srcX - zoomRadius, srcY - zoomRadius, zoomRadius * 2, zoomRadius * 2,
      0, 0, lw, lh
    );

    // Crosshair inside loupe
    lctx.strokeStyle = '#10b981';
    lctx.lineWidth = 1.5;
    lctx.beginPath();
    lctx.moveTo(lw / 2, 0); lctx.lineTo(lw / 2, lh);
    lctx.moveTo(0, lh / 2); lctx.lineTo(lw, lh / 2);
    lctx.stroke();
  }

  function initSmartCanvasEvents() {
    const canvas = $('smartCropCanvas');
    if (!canvas || canvas.dataset.bound) return;
    canvas.dataset.bound = '1';

    function getCanvasPos(e) {
      const rect = canvas.getBoundingClientRect();
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      const clientY = e.touches ? e.touches[0].clientY : e.clientY;
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      return {
        x: (clientX - rect.left) * scaleX,
        y: (clientY - rect.top) * scaleY
      };
    }

    function onStart(e) {
      if (!smartImg.width) return;
      const pos = getCanvasPos(e);
      const { pad, drawW, drawH } = smartGeom;
      const cPts = smartPts.map(p => ({ x: pad + p.x * drawW, y: pad + p.y * drawH }));

      // Find nearest corner (0..3)
      let bestIdx = -1;
      let bestDist = 36; // generous touch radius
      cPts.forEach((pt, i) => {
        const d = Math.hypot(pos.x - pt.x, pos.y - pt.y);
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      });

      // Check edge midpoints (4..7) if no corner was closer
      if (bestIdx === -1) {
        for (let i = 0; i < 4; i++) {
          const a = cPts[i], b = cPts[(i + 1) % 4];
          const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
          const d = Math.hypot(pos.x - mx, pos.y - my);
          if (d < 30) { bestDist = d; bestIdx = 4 + i; break; }
        }
      }

      if (bestIdx !== -1) {
        if (e.cancelable) e.preventDefault();
        activeDragIdx = bestIdx;
        onMove(e);
      }
    }

    function onMove(e) {
      if (activeDragIdx === -1) return;
      if (e.cancelable) e.preventDefault();
      const pos = getCanvasPos(e);
      const { pad, drawW, drawH } = smartGeom;
      const nx = Math.max(0.0, Math.min(1.0, (pos.x - pad) / drawW));
      const ny = Math.max(0.0, Math.min(1.0, (pos.y - pad) / drawH));

      if (activeDragIdx < 4) {
        smartPts[activeDragIdx] = { x: nx, y: ny };
        updateLoupe(smartPts[activeDragIdx]);
      } else {
        // Edge midpoint: move the two endpoints along the axis
        const edge = activeDragIdx - 4;
        const i1 = edge;
        const i2 = (edge + 1) % 4;
        const midX = (smartPts[i1].x + smartPts[i2].x) / 2;
        const midY = (smartPts[i1].y + smartPts[i2].y) / 2;
        const dx = nx - midX;
        const dy = ny - midY;
        smartPts[i1].x = Math.max(0, Math.min(1, smartPts[i1].x + dx));
        smartPts[i1].y = Math.max(0, Math.min(1, smartPts[i1].y + dy));
        smartPts[i2].x = Math.max(0, Math.min(1, smartPts[i2].x + dx));
        smartPts[i2].y = Math.max(0, Math.min(1, smartPts[i2].y + dy));
        updateLoupe({ x: nx, y: ny });
      }
      drawSmartCrop();
    }

    function onEnd() {
      activeDragIdx = -1;
      const loupe = $('smartLoupeCanvas');
      if (loupe) loupe.classList.add('hidden');
      drawSmartCrop();
    }

    canvas.addEventListener('mousedown', onStart);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onEnd);

    canvas.addEventListener('touchstart', onStart, { passive: false });
    window.addEventListener('touchmove', onMove, { passive: false });
    window.addEventListener('touchend', onEnd);
  }

  window.setSmartRatio = function(r) {
    smartTargetRatio = r;
    ['auto', 'nid', 'a4', 'passport', 'square'].forEach(k => {
      const b = $('smartBtn_' + k);
      if (!b) return;
      const on = k === r;
      b.className = on
        ? 'smart-ratio-btn py-1.5 px-1.5 bg-emerald-600 text-white font-bold rounded-lg shadow-sm'
        : 'smart-ratio-btn py-1.5 px-1.5 bg-gray-100 hover:bg-gray-200 text-gray-700 font-bold rounded-lg';
    });
  };

  window.setCropMode = function(mode) {
    cropMode = mode === 'normal' ? 'normal' : 'smart';
    const bSmart = $('btnCropModeSmart');
    const bNorm = $('btnCropModeNormal');
    const secSmart = $('smartCropSection');
    const secNorm = $('normalCropSection');

    if (bSmart && bNorm) {
      bSmart.className = cropMode === 'smart'
        ? 'py-1.5 rounded-lg font-bold bg-emerald-600 text-white shadow-sm flex items-center justify-center gap-1'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200 flex items-center justify-center gap-1';
      bNorm.className = cropMode === 'normal'
        ? 'py-1.5 rounded-lg font-bold bg-pink-600 text-white shadow-sm flex items-center justify-center gap-1'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200 flex items-center justify-center gap-1';
    }
    if (secSmart) secSmart.classList.toggle('hidden', cropMode !== 'smart');
    if (secNorm) secNorm.classList.toggle('hidden', cropMode !== 'normal');

    if (cropMode === 'normal') {
      initNormalCropper();
    } else {
      drawSmartCrop();
    }
  };

  function updateModalPdfPageLabels() {
    const total = Math.max(1, (uploadedFile && uploadedFile.page_count) ? uploadedFile.page_count : 1);
    const cropBar = $('pdfCropPageBar');
    const cropLbl = $('pdfCropPageLabel');
    if (cropBar) cropBar.classList.toggle('hidden', !(uploadedFile && uploadedFile.file_type === 'pdf' && total > 1));
    if (cropLbl) cropLbl.innerText = `📄 পাতা ${bn(modalPdfPage)} / ${bn(total)} ক্রপ করছেন`;

    const scanBar = $('pdfCamscanScopeBar');
    const scanLbl = $('pdfCamscanPageLabel');
    if (scanBar) scanBar.classList.toggle('hidden', !(uploadedFile && uploadedFile.file_type === 'pdf'));
    if (scanLbl) scanLbl.innerText = `📄 পাতা ${bn(modalPdfPage)} / ${bn(total)}`;
  }

  window.changeModalPdfPage = function(delta, whichModal) {
    if (!uploadedFile || uploadedFile.file_type !== 'pdf') return;
    const total = Math.max(1, uploadedFile.page_count || 1);
    const next = Math.max(1, Math.min(total, modalPdfPage + delta));
    if (next === modalPdfPage) return;
    modalPdfPage = next;
    docInlinePage = next;
    updateModalPdfPageLabels();
    if (whichModal === 'crop') {
      loadCropTargetImage();
    } else if (whichModal === 'camscan') {
      window.selectCamscanMode(currentCamscanMode);
      prefetchCamscanModes();
    }
  };

  window.setCamscanPdfScope = function(applyAll) {
    camscanApplyAllPages = !!applyAll;
    const bSingle = $('camScopeSingle');
    const bAll = $('camScopeAll');
    if (bSingle && bAll) {
      bSingle.className = !camscanApplyAllPages
        ? 'py-1 rounded-lg font-bold bg-emerald-600 text-white shadow-sm'
        : 'py-1 rounded-lg font-bold bg-white text-emerald-900 border border-emerald-300 hover:bg-emerald-100';
      bAll.className = camscanApplyAllPages
        ? 'py-1 rounded-lg font-bold bg-emerald-600 text-white shadow-sm'
        : 'py-1 rounded-lg font-bold bg-white text-emerald-900 border border-emerald-300 hover:bg-emerald-100';
    }
  };

  function initNormalCropper() {
    const target = $('cropImgTarget');
    if (cropper) {
      cropper.destroy();
      cropper = null;
    }
    const sideParam = (uploadedFile && uploadedFile.is_nid) ? `&side=${cropNidSide}` : '';
    const pageParam = (uploadedFile && uploadedFile.file_type === 'pdf') ? `&page=${modalPdfPage}` : '';
    const fastSrc = (uploadedFile && uploadedFile.file_type === 'image' && !uploadedFile.is_nid && instantLocalUrl)
      ? instantLocalUrl
      : `/api/preview/${encodeURIComponent(uploadedFile.file_id)}?w=1100&v=${fileRev}${sideParam}${pageParam}`;
    target.onload = () => {
      cropper = new Cropper(target, {
        viewMode: 1,
        autoCropArea: 0.92,
        responsive: true,
        background: false,
        ready() {
          setCropRatio(uploadedFile && uploadedFile.is_nid ? 'nid' : 'free');
        }
      });
    };
    target.src = fastSrc;
  }

  function loadCropTargetImage() {
    initSmartCanvasEvents();
    resetSmartPtsDefault();
    const sideParam = (uploadedFile && uploadedFile.is_nid) ? `&side=${cropNidSide}` : '';
    const pageParam = (uploadedFile && uploadedFile.file_type === 'pdf') ? `&page=${modalPdfPage}` : '';
    const imgUrl = (uploadedFile && uploadedFile.file_type === 'image' && !uploadedFile.is_nid && instantLocalUrl)
      ? instantLocalUrl
      : `/api/preview/${encodeURIComponent(uploadedFile.file_id)}?w=1100&v=${fileRev}${sideParam}${pageParam}`;

    smartImg = new Image();
    smartImg.crossOrigin = 'anonymous';
    smartImg.onload = () => {
      window.autoDetectSmartCorners(true);
    };
    smartImg.src = imgUrl;

    if (cropMode === 'normal') {
      initNormalCropper();
    }
  }

  window.switchNidCropSide = function(side) {
    cropNidSide = side === 'back' ? 'back' : 'front';
    const bf = $('btnCropSideFront');
    const bb = $('btnCropSideBack');
    if (bf && bb) {
      bf.className = cropNidSide === 'front'
        ? 'py-1.5 rounded-lg font-bold bg-pink-600 text-white shadow-sm'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200';
      bb.className = cropNidSide === 'back'
        ? 'py-1.5 rounded-lg font-bold bg-pink-600 text-white shadow-sm'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200';
    }
    loadCropTargetImage();
  };

  let pendingCropBlob = null;
  let pendingCropPreviewUrl = null;
  let baseWarpedCanvas = null;
  let currentCropEnhanceFx = 'none';

  // Fast 8x8 linear solver for 4-point projective homography (dst -> src)
  function solveHomography8x8(srcPts, dstW, dstH) {
    const dstPts = [
      [0, 0],
      [dstW, 0],
      [dstW, dstH],
      [0, dstH]
    ];
    const A = [];
    const B = [];
    for (let i = 0; i < 4; i++) {
      const x = dstPts[i][0], y = dstPts[i][1];
      const u = srcPts[i][0], v = srcPts[i][1];
      A.push([x, y, 1, 0, 0, 0, -x * u, -y * u]);
      B.push(u);
      A.push([0, 0, 0, x, y, 1, -x * v, -y * v]);
      B.push(v);
    }
    const n = 8;
    for (let i = 0; i < n; i++) {
      let maxRow = i;
      for (let k = i + 1; k < n; k++) {
        if (Math.abs(A[k][i]) > Math.abs(A[maxRow][i])) maxRow = k;
      }
      const tmpA = A[i]; A[i] = A[maxRow]; A[maxRow] = tmpA;
      const tmpB = B[i]; B[i] = B[maxRow]; B[maxRow] = tmpB;
      const piv = A[i][i] || 1e-12;
      for (let k = i + 1; k < n; k++) {
        const c = A[k][i] / piv;
        for (let j = i; j < n; j++) A[k][j] -= c * A[i][j];
        B[k] -= c * B[i];
      }
    }
    const h = new Array(8).fill(0);
    for (let i = n - 1; i >= 0; i--) {
      let s = B[i];
      for (let j = i + 1; j < n; j++) s -= A[i][j] * h[j];
      h[i] = s / (A[i][i] || 1e-12);
    }
    return h;
  }

  function warpPerspectiveClientSide(imgEl, pts, ratioKey) {
    const sw = imgEl.naturalWidth || imgEl.width;
    const sh = imgEl.naturalHeight || imgEl.height;
    const p = pts.map(pt => [pt.x * sw, pt.y * sh]);
    const wTop = Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]);
    const wBot = Math.hypot(p[2][0] - p[3][0], p[2][1] - p[3][1]);
    const hLeft = Math.hypot(p[3][0] - p[0][0], p[3][1] - p[0][1]);
    const hRight = Math.hypot(p[2][0] - p[1][0], p[2][1] - p[1][1]);

    let outW = Math.max(120, Math.round((wTop + wBot) * 0.5));
    let outH = Math.max(120, Math.round((hLeft + hRight) * 0.5));

    const rMap = { nid: 85.6 / 54.0, passport: 35.0 / 45.0, stamp: 20.0 / 25.0, square: 1.0, a4: 210.0 / 297.0 };
    const tr = rMap[ratioKey] || null;
    if (tr) {
      if (outW >= outH * tr) outH = Math.max(120, Math.round(outW / tr));
      else outW = Math.max(120, Math.round(outH * tr));
    }

    const maxDim = 1000;
    if (Math.max(outW, outH) > maxDim) {
      const sc = maxDim / Math.max(outW, outH);
      outW = Math.max(80, Math.round(outW * sc));
      outH = Math.max(80, Math.round(outH * sc));
    }

    const srcCvs = document.createElement('canvas');
    srcCvs.width = sw;
    srcCvs.height = sh;
    const sCtx = srcCvs.getContext('2d');
    sCtx.drawImage(imgEl, 0, 0);
    const srcData = sCtx.getImageData(0, 0, sw, sh).data;

    const dstCvs = document.createElement('canvas');
    dstCvs.width = outW;
    dstCvs.height = outH;
    const dCtx = dstCvs.getContext('2d');
    const dstImgData = dCtx.createImageData(outW, outH);
    const dstData = dstImgData.data;

    const h = solveHomography8x8(p, outW, outH);
    const h0 = h[0], h1 = h[1], h2 = h[2], h3 = h[3], h4 = h[4], h5 = h[5], h6 = h[6], h7 = h[7];

    let idx = 0;
    const maxX = sw - 1, maxY = sh - 1;
    for (let y = 0; y < outH; y++) {
      const hy1 = h1 * y + h2;
      const hy4 = h4 * y + h5;
      const hy7 = h7 * y + 1.0;
      for (let x = 0; x < outW; x++) {
        const w = 1.0 / (h6 * x + hy7);
        let u = (h0 * x + hy1) * w;
        let v = (h3 * x + hy4) * w;
        if (u < 0) u = 0; else if (u > maxX) u = maxX;
        if (v < 0) v = 0; else if (v > maxY) v = maxY;
        const x0 = u | 0, y0 = v | 0;
        const sIdx = (y0 * sw + x0) << 2;
        dstData[idx] = srcData[sIdx];
        dstData[idx + 1] = srcData[sIdx + 1];
        dstData[idx + 2] = srcData[sIdx + 2];
        dstData[idx + 3] = 255;
        idx += 4;
      }
    }
    dCtx.putImageData(dstImgData, 0, 0);
    return dstCvs;
  }

  function applyClientCropFilterToCanvas(baseCvs, fx) {
    if (!baseCvs || fx === 'none') return baseCvs;
    const w = baseCvs.width, h = baseCvs.height;
    const outCvs = document.createElement('canvas');
    outCvs.width = w;
    outCvs.height = h;
    const ctx = outCvs.getContext('2d');
    ctx.drawImage(baseCvs, 0, 0);
    const imgD = ctx.getImageData(0, 0, w, h);
    const d = imgD.data;

    // Sample 88th percentile luminance for white paper normalization
    const lumHist = new Uint32Array(256);
    const step = Math.max(4, ((d.length >> 2) / 8000) | 0) << 2;
    let totalSamples = 0;
    for (let i = 0; i < d.length; i += step) {
      const l = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
      lumHist[l]++;
      totalSamples++;
    }
    let acc = 0, whitePt = 225;
    const targetCnt = totalSamples * 0.88;
    for (let k = 0; k < 256; k++) {
      acc += lumHist[k];
      if (acc >= targetCnt) {
        whitePt = Math.max(155, k);
        break;
      }
    }

    const invScale = 255.0 / (whitePt * 0.94);
    for (let i = 0; i < d.length; i += 4) {
      let r = Math.min(255, d[i] * invScale);
      let g = Math.min(255, d[i + 1] * invScale);
      let b = Math.min(255, d[i + 2] * invScale);

      if (fx === 'bw_doc') {
        const l = (r * 0.299 + g * 0.587 + b * 0.114);
        let v = l > 202 ? 255 : (l < 115 ? Math.max(0, (l - 65) * 0.5) : ((l - 115) / 87.0) * 255);
        d[i] = d[i + 1] = d[i + 2] = v;
      } else if (fx === 'magic_color') {
        // S-curve + paper whitening
        r = r > 224 ? 255 : Math.max(0, Math.min(255, (r - 128) * 1.32 + 132));
        g = g > 224 ? 255 : Math.max(0, Math.min(255, (g - 128) * 1.32 + 132));
        b = b > 224 ? 255 : Math.max(0, Math.min(255, (b - 128) * 1.32 + 132));
        d[i] = r; d[i + 1] = g; d[i + 2] = b;
      } else if (fx === 'hd_clear') {
        r = Math.max(0, Math.min(255, (r - 128) * 1.18 + 132));
        g = Math.max(0, Math.min(255, (g - 128) * 1.18 + 132));
        b = Math.max(0, Math.min(255, (b - 128) * 1.18 + 132));
        d[i] = r; d[i + 1] = g; d[i + 2] = b;
      }
    }
    ctx.putImageData(imgD, 0, 0);
    return outCvs;
  }

  window.setCropEnhanceFilter = function(fx) {
    currentCropEnhanceFx = fx || 'none';
    const labels = {
      none: 'আসল ক্রপ',
      magic_color: '✨ ম্যাজিক ক্লিয়ার',
      hd_clear: '💎 HD ক্লিয়ার',
      bw_doc: '🖨️ সাদাকালো'
    };
    ['none', 'magic_color', 'hd_clear', 'bw_doc'].forEach(k => {
      const b = $('cropFx_' + k);
      if (!b) return;
      const on = k === currentCropEnhanceFx;
      b.className = on
        ? 'py-1.5 px-1.5 rounded-lg font-bold border-2 border-emerald-600 bg-emerald-50 text-emerald-900 shadow-sm'
        : 'py-1.5 px-1.5 rounded-lg font-bold border border-gray-200 bg-gray-50 text-gray-700 hover:border-emerald-500';
    });
    const badge = $('cropPreviewFilterBadge');
    if (badge) badge.innerText = labels[currentCropEnhanceFx] || 'প্রস্তুত';

    if (baseWarpedCanvas) {
      const outCvs = applyClientCropFilterToCanvas(baseWarpedCanvas, currentCropEnhanceFx);
      outCvs.toBlob((blob) => {
        if (!blob) return;
        pendingCropBlob = blob;
        if (pendingCropPreviewUrl) URL.revokeObjectURL(pendingCropPreviewUrl);
        pendingCropPreviewUrl = URL.createObjectURL(blob);
        const resImg = $('cropResultPreviewImg');
        if (resImg) resImg.src = pendingCropPreviewUrl;
      }, 'image/jpeg', 0.93);
    }
  };

  window.backToCropEditor = function() {
    if ($('cropLiveResultView')) $('cropLiveResultView').classList.add('hidden');
    if ($('cropFooterStep2')) $('cropFooterStep2').classList.add('hidden');
    if ($('cropEditorView')) $('cropEditorView').classList.remove('hidden');
    if ($('cropFooterStep1')) $('cropFooterStep1').classList.remove('hidden');
    if (cropMode === 'smart') {
      setTimeout(drawSmartCrop, 30);
    }
  };

  function showCropStep2LiveResult(blobUrl) {
    const resImg = $('cropResultPreviewImg');
    if (resImg) resImg.src = blobUrl;
    if ($('cropEditorView')) $('cropEditorView').classList.add('hidden');
    if ($('cropFooterStep1')) $('cropFooterStep1').classList.add('hidden');
    if ($('cropLiveResultView')) $('cropLiveResultView').classList.remove('hidden');
    if ($('cropFooterStep2')) $('cropFooterStep2').classList.remove('hidden');
  }

  window.openCropModal = function(side) {
    if (!uploadedFile || (uploadedFile.file_type !== 'image' && uploadedFile.file_type !== 'pdf')) return;
    const modal = $('cropModal');
    const nidSwitch = $('nidCropSideSwitch');
    if (nidSwitch) nidSwitch.classList.toggle('hidden', !uploadedFile.is_nid);
    modalPdfPage = Math.max(1, Math.min(uploadedFile.page_count || 1, docInlinePage || 1));
    updateModalPdfPageLabels();
    modal.classList.remove('hidden');

    currentCropEnhanceFx = 'none';
    window.setCropEnhanceFilter('none');
    window.backToCropEditor();
    window.setSmartRatio(uploadedFile.is_nid ? 'nid' : 'auto');
    window.setCropMode('smart');

    const chosenSide = (typeof side === 'string' && (side === 'front' || side === 'back')) ? side : 'front';
    if (uploadedFile.is_nid) {
      window.switchNidCropSide(chosenSide);
    } else {
      loadCropTargetImage();
    }
  };

  window.closeCropModal = function() {
    const modal = $('cropModal');
    if (modal) modal.classList.add('hidden');
    if (cropper) {
      cropper.destroy();
      cropper = null;
    }
    if (pendingCropPreviewUrl) {
      URL.revokeObjectURL(pendingCropPreviewUrl);
      pendingCropPreviewUrl = null;
    }
    pendingCropBlob = null;
    baseWarpedCanvas = null;
  };

  window.setCropRatio = function(ratio) {
    activeRatio = ratio;
    const btns = {
      free: $('cropBtnFree'),
      nid: $('cropBtnNid'),
      passport: $('cropBtnPassport'),
      stamp: $('cropBtnStamp'),
      square: $('cropBtnSquare')
    };
    Object.keys(btns).forEach(k => {
      const b = btns[k];
      if (!b) return;
      const on = k === ratio;
      b.className = on
        ? 'crop-ratio-btn py-1.5 px-2 bg-pink-600 text-white font-bold rounded-lg shadow-sm'
        : 'crop-ratio-btn py-1.5 px-2 bg-gray-100 hover:bg-gray-200 text-gray-700 font-bold rounded-lg';
    });

    if (!cropper) return;
    if (ratio === 'nid') cropper.setAspectRatio(85.6 / 54);
    else if (ratio === 'passport') cropper.setAspectRatio(35 / 45);
    else if (ratio === 'stamp') cropper.setAspectRatio(20 / 25);
    else if (ratio === 'square') cropper.setAspectRatio(1);
    else cropper.setAspectRatio(NaN);
  };

  window.rotateCropper = function(deg) {
    if (cropper) cropper.rotate(deg);
  };

  // Step 1 -> Step 2: Generate straightened/cropped image INSTANTLY in-browser (<35ms) and show it LIVE inside the modal
  window.previewCroppedImage = async function() {
    if (!uploadedFile) return;
    const btn = $('btnPreviewCrop');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'সোজা ও ক্রপ করা হচ্ছে...';
    }

    if (cropMode === 'smart') {
      try {
        // Fast path: Instant client-side 4-point perspective warp (0ms network wait!)
        if (smartImg && smartImg.complete && (smartImg.naturalWidth || smartImg.width) > 0) {
          baseWarpedCanvas = warpPerspectiveClientSide(smartImg, smartPts, smartTargetRatio);
          const outCvs = applyClientCropFilterToCanvas(baseWarpedCanvas, currentCropEnhanceFx);
          outCvs.toBlob((blob) => {
            if (btn) {
              btn.disabled = false;
              btn.innerHTML = '<span>✂️</span> <span>ক্রপ ও সোজা করুন</span>';
            }
            if (!blob) return;
            pendingCropBlob = blob;
            if (pendingCropPreviewUrl) URL.revokeObjectURL(pendingCropPreviewUrl);
            pendingCropPreviewUrl = URL.createObjectURL(blob);
            showCropStep2LiveResult(pendingCropPreviewUrl);
          }, 'image/jpeg', 0.93);
          return;
        }

        const res = await fetch('/api/image/perspective-crop-preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file_id: uploadedFile.file_id,
            side: cropNidSide,
            page: modalPdfPage,
            points: smartPts,
            ratio: smartTargetRatio,
            enhance_mode: currentCropEnhanceFx
          })
        });
        if (!res.ok) {
          let errMsg = 'স্মার্ট ক্রপ ব্যর্থ হয়েছে';
          try { const j = await res.json(); errMsg = j.detail || errMsg; } catch (e) {}
          alert(errMsg);
          return;
        }
        const blob = await res.blob();
        pendingCropBlob = blob;
        if (pendingCropPreviewUrl) URL.revokeObjectURL(pendingCropPreviewUrl);
        pendingCropPreviewUrl = URL.createObjectURL(blob);
        showCropStep2LiveResult(pendingCropPreviewUrl);
      } catch (e) {
        alert('সার্ভার এরর: ' + e);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '<span>✂️</span> <span>ক্রপ ও সোজা করুন</span>';
        }
      }
      return;
    }

    // Normal Cropper.js mode
    if (!cropper) {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✂️</span> <span>ক্রপ ও সোজা করুন</span>';
      }
      return;
    }

    baseWarpedCanvas = cropper.getCroppedCanvas({ maxWidth: 2200, maxHeight: 2200 });
    if (!baseWarpedCanvas) {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✂️</span> <span>ক্রপ ও সোজা করুন</span>';
      }
      alert('ক্যানভাস থেকে ছবি তৈরি করা যায়নি।');
      return;
    }
    const outCvs = applyClientCropFilterToCanvas(baseWarpedCanvas, currentCropEnhanceFx);
    outCvs.toBlob((blob) => {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✂️</span> <span>ক্রপ ও সোজা করুন</span>';
      }
      if (!blob) {
        alert('ক্যানভাস থেকে ছবি তৈরি করা যায়নি।');
        return;
      }
      pendingCropBlob = blob;
      if (pendingCropPreviewUrl) URL.revokeObjectURL(pendingCropPreviewUrl);
      pendingCropPreviewUrl = URL.createObjectURL(blob);
      showCropStep2LiveResult(pendingCropPreviewUrl);
    }, 'image/jpeg', 0.95);
  };

  // Step 2 "OK" button: Save cropped/straightened image and go back to print page with live preview updated
  window.confirmAndSaveCrop = async function() {
    if (!uploadedFile) return;
    const btn = $('btnConfirmCropOk');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'সেভ হচ্ছে...';
    }

    try {
      if (cropMode === 'smart') {
        const res = await fetch('/api/image/perspective-crop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file_id: uploadedFile.file_id,
            side: cropNidSide,
            page: modalPdfPage,
            points: smartPts,
            ratio: smartTargetRatio,
            enhance_mode: currentCropEnhanceFx
          })
        });
        const data = await res.json();
        if (!data.success) {
          alert('স্মার্ট ক্রপ সেভ ব্যর্থ হয়েছে: ' + (data.detail || ''));
          return;
        }
      } else {
        if (!pendingCropBlob) {
          alert('ক্রপ করা ছবি পাওয়া যায়নি।');
          return;
        }
        const fd = new FormData();
        fd.append('file_id', uploadedFile.file_id);
        fd.append('side', cropNidSide);
        fd.append('page', String(modalPdfPage));
        fd.append('file', pendingCropBlob, 'cropped.jpg');

        const res = await fetch('/api/image/save-crop', {
          method: 'POST',
          body: fd
        });
        const data = await res.json();
        if (!data.success) {
          alert('ক্রপ সংরক্ষণ ব্যর্থ হয়েছে: ' + (data.detail || ''));
          return;
        }
      }

      bumpFileRev();
      if (uploadedFile.file_type === 'image' && !uploadedFile.is_nid && pendingCropBlob) {
        instantLocalUrl = URL.createObjectURL(pendingCropBlob);
        syncActiveStudioPhotoUrl();
      } else if (uploadedFile.file_type === 'pdf') {
        docInlinePage = modalPdfPage;
      }
      closeCropModal();
      refreshTopLivePreview(true);
      if (currentMode === 'studio') scheduleStudio();
    } catch (e) {
      alert('সার্ভার এরর: ' + e);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✓</span> <span>ওকে (প্রিন্ট পেইজে যান)</span>';
      }
    }
  };

  // ── CamScanner Enhancer with Instant Blob Cache & Strength Control ──
  let currentCamscanMode = 'magic_color';
  let currentCamscanStrength = 'normal';
  const camscanBlobCache = new Map();

  const CAM_LABELS = {
    original: '🖼️ আসল ছবি (Original)',
    magic_color: '✨ ম্যাজিক কালার',
    hd_clear: '💎 HD ক্লিয়ার (আইডি/ডকুমেন্ট)',
    bw_doc: '🖨️ ফটোকপি (B&W)',
    grayscale: '◐ গ্রে-স্কেল'
  };

  function getCamscanCacheKey(mode, strength) {
    if (!uploadedFile) return '';
    const effStr = mode === 'original' ? 'normal' : (strength || 'normal');
    const pg = uploadedFile.file_type === 'pdf' ? modalPdfPage : 1;
    return `${uploadedFile.file_id}|${fileRev}|${pg}|${mode}|${effStr}`;
  }

  function prefetchCamscanModes() {
    if (!uploadedFile) return;
    const modesToPreload = ['magic_color', 'hd_clear', 'bw_doc', 'original', 'grayscale'];
    modesToPreload.forEach((m, idx) => {
      const key = getCamscanCacheKey(m, currentCamscanStrength);
      if (camscanBlobCache.has(key)) return;
      setTimeout(() => {
        if (!uploadedFile || camscanBlobCache.has(key)) return;
        const url = `/api/image/camscan-preview?file_id=${encodeURIComponent(uploadedFile.file_id)}&mode=${encodeURIComponent(m)}&strength=${encodeURIComponent(currentCamscanStrength)}&page=${modalPdfPage}&v=${fileRev}`;
        fetch(url)
          .then(r => r.ok ? r.blob() : null)
          .then(b => {
            if (b) camscanBlobCache.set(key, URL.createObjectURL(b));
          })
          .catch(() => {});
      }, idx * 120);
    });
  }

  window.openCamscanModal = function() {
    if (!uploadedFile || (uploadedFile.file_type !== 'image' && uploadedFile.file_type !== 'pdf')) return;
    modalPdfPage = Math.max(1, Math.min(uploadedFile.page_count || 1, docInlinePage || 1));
    updateModalPdfPageLabels();
    window.setCamscanPdfScope(false);
    const modal = $('camscanModal');
    if (modal) modal.classList.remove('hidden');
    window.setCamscanStrength(currentCamscanStrength, true);
    window.selectCamscanMode('magic_color');
    prefetchCamscanModes();
  };

  window.closeCamscanModal = function() {
    const modal = $('camscanModal');
    if (modal) modal.classList.add('hidden');
  };

  window.setCamscanStrength = function(strength, skipReload) {
    currentCamscanStrength = ['light', 'normal', 'strong'].includes(strength) ? strength : 'normal';
    ['light', 'normal', 'strong'].forEach(s => {
      const b = $('camStr_' + s);
      if (!b) return;
      const on = s === currentCamscanStrength;
      b.className = on
        ? 'py-1 px-2 rounded-lg font-bold border-2 border-emerald-600 bg-emerald-50 text-emerald-900 shadow-sm'
        : 'py-1 px-2 rounded-lg font-bold border border-gray-200 bg-white text-gray-600 hover:border-emerald-400';
    });
    if (!skipReload && currentCamscanMode !== 'original') {
      window.selectCamscanMode(currentCamscanMode);
    }
  };

  window.selectCamscanMode = function(mode) {
    if (!uploadedFile) return;
    currentCamscanMode = mode;
    ['original', 'magic_color', 'hd_clear', 'bw_doc', 'grayscale'].forEach(m => {
      const b = $('camBtn_' + m);
      if (!b) return;
      const on = m === mode;
      b.className = on
        ? 'cam-mode-btn p-1.5 border-2 border-emerald-600 bg-emerald-50 text-emerald-900 rounded-xl font-bold shadow-sm'
        : 'cam-mode-btn p-1.5 border-2 border-gray-200 hover:border-emerald-500 rounded-xl font-bold bg-gray-50 text-gray-700';
    });

    const strBar = $('camscanStrengthBar');
    if (strBar) {
      strBar.style.opacity = mode === 'original' ? '0.45' : '1';
      strBar.style.pointerEvents = mode === 'original' ? 'none' : 'auto';
    }

    const badge = $('camscanModeBadge');
    if (badge) badge.innerText = CAM_LABELS[mode] || mode;

    const img = $('camscanPreviewImg');
    const loader = $('camscanLoading');
    if (!img || !loader) return;

    const cacheKey = getCamscanCacheKey(mode, currentCamscanStrength);
    if (camscanBlobCache.has(cacheKey)) {
      loader.classList.add('hidden');
      img.classList.remove('hidden');
      img.src = camscanBlobCache.get(cacheKey);
      return;
    }

    loader.classList.remove('hidden');
    img.classList.add('hidden');
    const previewUrl = `/api/image/camscan-preview?file_id=${encodeURIComponent(uploadedFile.file_id)}&mode=${encodeURIComponent(mode)}&strength=${encodeURIComponent(currentCamscanStrength)}&page=${modalPdfPage}&v=${fileRev}`;
    fetch(previewUrl)
      .then(r => {
        if (!r.ok) throw new Error('Preview error');
        return r.blob();
      })
      .then(blob => {
        const objUrl = URL.createObjectURL(blob);
        camscanBlobCache.set(cacheKey, objUrl);
        if (currentCamscanMode === mode) {
          img.src = objUrl;
          loader.classList.add('hidden');
          img.classList.remove('hidden');
        }
      })
      .catch(() => {
        loader.innerText = '⚠️ প্রিভিউ লোড করা যায়নি';
      });
  };

  window.saveCamscanEffect = async function() {
    if (!uploadedFile) return;
    const btn = $('btnSaveCamscan');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'সেভ হচ্ছে...';
    }
    try {
      const res = await fetch('/api/image/camscan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_id: uploadedFile.file_id,
          mode: currentCamscanMode,
          strength: currentCamscanStrength,
          page: modalPdfPage,
          apply_all: camscanApplyAllPages
        })
      });
      const data = await res.json();
      if (data.success) {
        const cachedUrl = camscanBlobCache.get(getCamscanCacheKey(currentCamscanMode, currentCamscanStrength));
        if (uploadedFile.file_type === 'image' && !uploadedFile.is_nid && cachedUrl) {
          instantLocalUrl = cachedUrl;
        } else if (uploadedFile.file_type === 'pdf') {
          docInlinePage = modalPdfPage;
        }
        bumpFileRev();
        syncActiveStudioPhotoUrl();
        closeCamscanModal();
        refreshTopLivePreview(true);
        if (currentMode === 'studio') scheduleStudio();
      } else {
        alert('স্ক্যানার ইফেক্ট ব্যর্থ: ' + (data.detail || ''));
      }
    } catch (e) {
      alert('স্ক্যানার ইফেক্ট প্রয়োগ ব্যর্থ হয়েছে: ' + e);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✓</span> <span>স্ক্যান সেভ করুন</span>';
      }
    }
  };

  // ── Interactive Word / Excel / Document Editor Modal Logic ──
  let officeEditorState = {
    doc_kind: 'word',
    title: 'Document',
    orientation: 'portrait',
    html: '',
    sheets: [],
    activeSheetIdx: 0
  };

  function syncActiveExcelDomToState() {
    if (officeEditorState.doc_kind !== 'excel' || !Array.isArray(officeEditorState.sheets)) return;
    const s = officeEditorState.sheets[officeEditorState.activeSheetIdx];
    if (!s) return;
    const tbl = $('excelGridTable');
    if (!tbl) return;
    const newRows = [];
    tbl.querySelectorAll('tr[data-row]').forEach(tr => {
      const rowVals = [];
      tr.querySelectorAll('input[data-col]').forEach(inp => {
        rowVals.push(inp.value || '');
      });
      newRows.push(rowVals);
    });
    if (newRows.length > 0) {
      s.rows = newRows;
    }
  }

  function renderExcelGridInModal() {
    const tbl = $('excelGridTable');
    if (!tbl || !Array.isArray(officeEditorState.sheets)) return;
    const s = officeEditorState.sheets[officeEditorState.activeSheetIdx] || { name: 'Sheet 1', rows: [['']] };
    const rows = Array.isArray(s.rows) && s.rows.length > 0 ? s.rows : [['', '', '']];
    const colCount = Math.max(1, ...rows.map(r => (r ? r.length : 1)));

    let html = '<thead><tr class="bg-slate-200 text-slate-700 font-bold"><th class="border border-slate-300 px-2 py-1 w-9 text-center">#</th>';
    for (let c = 0; c < colCount; c++) {
      const colLetter = String.fromCharCode(65 + (c % 26)) + (c >= 26 ? Math.floor(c / 26) : '');
      html += `<th class="border border-slate-300 px-2 py-1 min-w-[95px] text-center">${colLetter}</th>`;
    }
    html += '</tr></thead><tbody>';

    rows.forEach((r, rIdx) => {
      const isHeader = rIdx === 0 && rows.length > 1;
      html += `<tr data-row="${rIdx}" class="${isHeader ? 'bg-amber-50/70 font-bold' : 'hover:bg-slate-50'}">`;
      html += `<td class="border border-slate-300 px-1.5 py-1 text-center text-gray-500 font-mono bg-slate-100">${rIdx + 1}</td>`;
      for (let c = 0; c < colCount; c++) {
        const val = (r && r[c] !== undefined && r[c] !== null) ? String(r[c]).replace(/"/g, '&quot;') : '';
        html += `<td class="border border-slate-300 p-0"><input type="text" data-col="${c}" value="${val}" class="w-full px-2 py-1.5 text-xs bg-transparent focus:bg-amber-50 focus:outline-none ${isHeader ? 'font-bold text-gray-900' : 'text-gray-800'}"></td>`;
      }
      html += '</tr>';
    });
    html += '</tbody>';
    tbl.innerHTML = html;
  }

  window.openOfficeEditModal = async function() {
    if (!uploadedFile || uploadedFile.file_type !== 'pdf') return;
    const modal = $('officeEditModal');
    if (modal) modal.classList.remove('hidden');
    const wordArea = $('wordEditorArea');
    const excelArea = $('excelEditorArea');
    const wordTb = $('wordEditorToolbar');
    const excelTb = $('excelEditorToolbar');
    if (wordArea) wordArea.innerHTML = '<p class="text-gray-500 animate-pulse">⏳ ডকুমেন্ট লোড হচ্ছে...</p>';

    try {
      const res = await fetch(`/api/office/content/${encodeURIComponent(uploadedFile.file_id)}?t=${Date.now()}`);
      const data = await res.json();
      if (!res.ok || !data.success) {
        alert(data.detail || 'ডকুমেন্ট লোড করা যায়নি');
        closeOfficeEditModal();
        return;
      }

      officeEditorState = {
        doc_kind: data.doc_kind || 'word',
        title: data.title || uploadedFile.original_filename || 'Document',
        orientation: data.orientation || (checked('orientation') || 'portrait'),
        margins: data.margins || [36.0, 36.0, 36.0, 36.0],
        html: data.html || '<p>&nbsp;</p>',
        sheets: Array.isArray(data.sheets) && data.sheets.length > 0 ? data.sheets : [{ name: 'Sheet 1', rows: [['', '', '']] }],
        activeSheetIdx: 0
      };

      const titleEl = $('officeEditModalTitle');
      if (titleEl) {
        titleEl.innerText = officeEditorState.doc_kind === 'excel'
          ? `📊 Excel স্প্রেডশিট এডিটর — ${officeEditorState.title}`
          : `📝 Word / ডকুমেন্ট এডিটর — ${officeEditorState.title}`;
      }

      const orientSel = $('officeEditOrientation');
      if (orientSel) orientSel.value = officeEditorState.orientation;

      const isExcel = officeEditorState.doc_kind === 'excel';
      if (wordArea) wordArea.classList.toggle('hidden', isExcel);
      if (excelArea) excelArea.classList.toggle('hidden', !isExcel);
      if (wordTb) wordTb.classList.toggle('hidden', isExcel);
      if (excelTb) excelTb.classList.toggle('hidden', !isExcel);

      if (isExcel) {
        const sheetSel = $('excelSheetSelect');
        if (sheetSel) {
          sheetSel.innerHTML = officeEditorState.sheets
            .map((s, idx) => `<option value="${idx}">📊 ${s.name || ('Sheet ' + (idx + 1))}</option>`)
            .join('');
          sheetSel.value = '0';
        }
        renderExcelGridInModal();
      } else if (wordArea) {
        wordArea.innerHTML = officeEditorState.html;
        setupTableInteractiveResizer();
      }
    } catch (e) {
      alert('ডকুমেন্ট লোড ব্যর্থ হয়েছে: ' + e);
      closeOfficeEditModal();
    }
  };

  window.closeOfficeEditModal = function() {
    const modal = $('officeEditModal');
    if (modal) modal.classList.add('hidden');
  };

  window.execWordCmd = function(cmd, val = null) {
    const area = $('wordEditorArea');
    if (area) area.focus();
    document.execCommand(cmd, false, val);
  };

  window.applyWordFormatBlock = function(tag) {
    const area = $('wordEditorArea');
    if (area) area.focus();
    document.execCommand('formatBlock', false, tag);
  };

  window.applyWordFontSize = function(size) {
    const area = $('wordEditorArea');
    if (area) area.focus();
    document.execCommand('fontSize', false, size);
  };

  function getCurrentTableCell() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    let node = sel.getRangeAt(0).commonAncestorContainer;
    const area = $('wordEditorArea');
    while (node && node !== area) {
      if (node.nodeName === 'TD' || node.nodeName === 'TH') return node;
      node = node.parentNode;
    }
    return null;
  }

  function redistributeColgroup(tbl) {
    const cg = tbl.querySelector('colgroup');
    if (!cg) return;
    const cols = Array.from(cg.children);
    if (!cols.length) return;
    const pct = (100 / cols.length).toFixed(2);
    cols.forEach((c, i) => {
      c.style.width = (i === cols.length - 1 ? (100 - pct * (cols.length - 1)).toFixed(2) : pct) + '%';
    });
  }

  window.insertWordTable = function() {
    const area = $('wordEditorArea');
    if (!area) return;
    area.focus();
    const tblHtml = '<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse; width:100%; table-layout:fixed; margin:8px 0;"><colgroup><col style="width:33.33%;"><col style="width:33.33%;"><col style="width:33.34%;"></colgroup><tr><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;"><b>কলাম ১</b></td><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;"><b>কলাম ২</b></td><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;"><b>কলাম ৩</b></td></tr><tr><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;">&nbsp;</td><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;">&nbsp;</td><td style="border:1px solid #1e293b; padding:2.5pt 5.4pt;">&nbsp;</td></tr></table><p>&nbsp;</p>';
    document.execCommand('insertHTML', false, tblHtml);
    setupTableInteractiveResizer();
  };

  window.addTableRowAbove = function() {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে রো-এর উপরে নতুন রো যোগ করতে চান সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const tr = td.parentElement;
    const newTr = tr.cloneNode(true);
    for (let i = 0; i < newTr.children.length; i++) {
      newTr.children[i].innerHTML = '&nbsp;';
    }
    tr.parentElement.insertBefore(newTr, tr);
  };

  window.addTableRowBelow = function() {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে রো-এর নিচে নতুন রো যোগ করতে চান সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const tr = td.parentElement;
    const newTr = tr.cloneNode(true);
    for (let i = 0; i < newTr.children.length; i++) {
      newTr.children[i].innerHTML = '&nbsp;';
    }
    if (tr.nextSibling) {
      tr.parentElement.insertBefore(newTr, tr.nextSibling);
    } else {
      tr.parentElement.appendChild(newTr);
    }
  };

  window.deleteTableRow = function() {
    const td = getCurrentTableCell();
    if (!td) return;
    const tr = td.parentElement;
    const tbody = tr.parentElement;
    if (tbody && tbody.children.length > 1) {
      tr.remove();
    } else {
      alert('টেবিলে অন্তত একটি রো থাকতে হবে।');
    }
  };

  window.addTableColLeft = function() {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে কলামের বামে নতুন কলাম যোগ করতে চান সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const colIdx = td.cellIndex;
    const tbl = td.closest('table');
    if (!tbl) return;
    const rows = tbl.querySelectorAll('tr');
    rows.forEach(r => {
      const newCell = document.createElement(r.children[colIdx] ? r.children[colIdx].nodeName : 'td');
      newCell.style.border = '1px solid #1e293b';
      newCell.style.padding = '2.5pt 5.4pt';
      newCell.innerHTML = '&nbsp;';
      if (r.children[colIdx]) {
        r.insertBefore(newCell, r.children[colIdx]);
      } else {
        r.appendChild(newCell);
      }
    });
    const cg = tbl.querySelector('colgroup');
    if (cg) {
      const col = document.createElement('col');
      if (cg.children[colIdx]) cg.insertBefore(col, cg.children[colIdx]);
      else cg.appendChild(col);
      redistributeColgroup(tbl);
    }
  };

  window.addTableColRight = function() {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে কলামের ডানে নতুন কলাম যোগ করতে চান সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const colIdx = td.cellIndex;
    const tbl = td.closest('table');
    if (!tbl) return;
    const rows = tbl.querySelectorAll('tr');
    rows.forEach(r => {
      const newCell = document.createElement(r.children[colIdx] ? r.children[colIdx].nodeName : 'td');
      newCell.style.border = '1px solid #1e293b';
      newCell.style.padding = '2.5pt 5.4pt';
      newCell.innerHTML = '&nbsp;';
      if (r.children[colIdx + 1]) {
        r.insertBefore(newCell, r.children[colIdx + 1]);
      } else {
        r.appendChild(newCell);
      }
    });
    const cg = tbl.querySelector('colgroup');
    if (cg) {
      const col = document.createElement('col');
      if (cg.children[colIdx + 1]) cg.insertBefore(col, cg.children[colIdx + 1]);
      else cg.appendChild(col);
      redistributeColgroup(tbl);
    }
  };

  window.deleteTableCol = function() {
    const td = getCurrentTableCell();
    if (!td) return;
    const colIdx = td.cellIndex;
    const tbl = td.closest('table');
    if (!tbl) return;
    const rows = tbl.querySelectorAll('tr');
    if (rows[0] && rows[0].children.length <= 1) {
      alert('টেবিলে অন্তত একটি কলাম থাকতে হবে।');
      return;
    }
    rows.forEach(r => {
      if (r.children[colIdx]) r.children[colIdx].remove();
    });
    const cg = tbl.querySelector('colgroup');
    if (cg && cg.children[colIdx]) {
      cg.children[colIdx].remove();
      redistributeColgroup(tbl);
    }
  };

  window.setTableCellBackground = function(color) {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে সেলের ব্যাকগ্রাউন্ডের রং পরিবর্তন করতে চান, সেই ঘরে কার্সর রাখুন।');
      return;
    }
    td.style.backgroundColor = color;
  };

  window.adjustCurrentRowHeight = function(delta) {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে সারির উচ্চতা পরিবর্তন করতে চান, সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const tr = td.parentElement;
    const currentH = tr.offsetHeight || 24;
    const newH = Math.max(18, currentH + delta);
    tr.style.height = newH + 'px';
    Array.from(tr.children).forEach(cell => {
      cell.style.height = newH + 'px';
    });
  };

  window.adjustCurrentColWidth = function(delta) {
    const td = getCurrentTableCell();
    if (!td) {
      alert('যে কলামের প্রস্থ পরিবর্তন করতে চান, সেই ঘরে কার্সর রাখুন।');
      return;
    }
    const colIdx = td.cellIndex;
    const tbl = td.closest('table');
    if (!tbl) return;
    const cg = tbl.querySelector('colgroup');
    if (!cg || !cg.children[colIdx]) return;
    const totalCols = cg.children.length;
    const nextIdx = (colIdx < totalCols - 1) ? (colIdx + 1) : (colIdx - 1);
    if (nextIdx < 0 || nextIdx >= totalCols) return;

    const tblW = tbl.offsetWidth || 500;
    const deltaPct = (delta / tblW) * 100;
    const p1 = parseFloat(cg.children[colIdx].style.width) || (100 / totalCols);
    const p2 = parseFloat(cg.children[nextIdx].style.width) || (100 / totalCols);
    const combined = p1 + p2;
    const newP1 = Math.max(4, Math.min(combined - 4, p1 + deltaPct));
    const newP2 = combined - newP1;

    cg.children[colIdx].style.width = newP1.toFixed(2) + '%';
    cg.children[nextIdx].style.width = newP2.toFixed(2) + '%';
  };

  function setupTableInteractiveResizer() {
    const area = $('wordEditorArea');
    if (!area) return;
    if (area._resizerAttached) return;
    area._resizerAttached = true;

    let resizingType = null;
    let activeCell = null;
    let activeRow = null;
    let startX = 0, startY = 0;
    let startHeight = 0;
    let activeTable = null;
    let activeColIndex = 0;
    let nextColIndex = -1;
    let initPct1 = 0, initPct2 = 0, combinedPct = 0;

    area.addEventListener('mousemove', function(e) {
      if (resizingType) return;
      const td = e.target.closest('td, th');
      if (!td || !area.contains(td)) {
        document.body.style.cursor = '';
        return;
      }
      const rect = td.getBoundingClientRect();
      const nearBottom = Math.abs(rect.bottom - e.clientY) <= 5;
      const nearRight = Math.abs(rect.right - e.clientX) <= 5;

      if (nearBottom) {
        td.style.cursor = 'row-resize';
      } else if (nearRight) {
        td.style.cursor = 'col-resize';
      } else {
        td.style.cursor = '';
      }
    });

    area.addEventListener('mousedown', function(e) {
      const td = e.target.closest('td, th');
      if (!td || !area.contains(td)) return;
      const rect = td.getBoundingClientRect();
      const nearBottom = Math.abs(rect.bottom - e.clientY) <= 5;
      const nearRight = Math.abs(rect.right - e.clientX) <= 5;

      if (nearBottom) {
        e.preventDefault();
        resizingType = 'row';
        activeCell = td;
        activeRow = td.parentElement;
        activeTable = td.closest('table');
        startY = e.clientY;
        startHeight = activeRow.offsetHeight || td.offsetHeight || 24;
        document.body.style.cursor = 'row-resize';
        document.body.style.userSelect = 'none';
      } else if (nearRight) {
        e.preventDefault();
        resizingType = 'col';
        activeCell = td;
        activeTable = td.closest('table');
        activeColIndex = td.cellIndex;
        startX = e.clientX;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        if (activeTable) {
          const firstRow = activeTable.rows[0];
          const colsCount = firstRow ? firstRow.children.length : 1;
          const tblWidth = activeTable.offsetWidth || 500;
          let cg = activeTable.querySelector('colgroup');
          if (!cg) {
            cg = document.createElement('colgroup');
            for (let c = 0; c < colsCount; c++) {
              const cell = firstRow.children[c];
              const cellW = cell ? (cell.offsetWidth || cell.getBoundingClientRect().width) : (tblWidth / colsCount);
              const p = Math.max(3, (cellW / tblWidth) * 100);
              const colEl = document.createElement('col');
              colEl.style.width = p.toFixed(2) + '%';
              cg.appendChild(colEl);
            }
            activeTable.insertBefore(cg, activeTable.firstChild);
          }
          const totalCols = cg.children.length;
          nextColIndex = (activeColIndex < totalCols - 1) ? (activeColIndex + 1) : -1;
          if (nextColIndex >= 0) {
            initPct1 = parseFloat(cg.children[activeColIndex].style.width) || (100 / totalCols);
            initPct2 = parseFloat(cg.children[nextColIndex].style.width) || (100 / totalCols);
            combinedPct = initPct1 + initPct2;
          }
        }
      }
    });

    window.addEventListener('mousemove', function(e) {
      if (!resizingType) return;
      if (resizingType === 'row' && activeRow) {
        const diffY = e.clientY - startY;
        const newH = Math.max(18, startHeight + diffY);
        activeRow.style.height = newH + 'px';
        Array.from(activeRow.children).forEach(cell => {
          cell.style.height = newH + 'px';
          cell.style.border = '1px solid #1e293b';
          cell.style.boxSizing = 'border-box';
        });
      } else if (resizingType === 'col' && activeCell && activeTable && nextColIndex >= 0) {
        const diffX = e.clientX - startX;
        const tblWidth = activeTable.offsetWidth || 500;
        const diffPct = (diffX / tblWidth) * 100;
        const cg = activeTable.querySelector('colgroup');
        if (cg && cg.children[activeColIndex] && cg.children[nextColIndex]) {
          const newPct1 = Math.max(3, Math.min(combinedPct - 3, initPct1 + diffPct));
          const newPct2 = combinedPct - newPct1;
          cg.children[activeColIndex].style.width = newPct1.toFixed(2) + '%';
          cg.children[nextColIndex].style.width = newPct2.toFixed(2) + '%';

          // Keep table width bounded so borders never get clipped
          activeTable.style.maxWidth = '100%';
          activeTable.style.borderCollapse = 'collapse';

          // Synchronize explicit point widths on cells in all rows so PyMuPDF Story renders exact resized widths
          const ptsPerPercent = (tblWidth * 0.75) / 100.0;
          const wPt1 = Math.max(10, (newPct1 * ptsPerPercent) - 11.8);
          const wPt2 = Math.max(10, (newPct2 * ptsPerPercent) - 11.8);

          for (let r = 0; r < activeTable.rows.length; r++) {
            const rowCells = activeTable.rows[r].children;
            if (rowCells[activeColIndex]) {
              rowCells[activeColIndex].style.width = wPt1.toFixed(1) + 'pt';
              rowCells[activeColIndex].style.border = '1px solid #1e293b';
              rowCells[activeColIndex].style.boxSizing = 'border-box';
            }
            if (rowCells[nextColIndex]) {
              rowCells[nextColIndex].style.width = wPt2.toFixed(1) + 'pt';
              rowCells[nextColIndex].style.border = '1px solid #1e293b';
              rowCells[nextColIndex].style.boxSizing = 'border-box';
            }
          }
        }
      }
    });

    window.addEventListener('mouseup', function() {
      if (resizingType) {
        if (activeTable) {
          activeTable.style.maxWidth = '100%';
          activeTable.style.borderCollapse = 'collapse';
          const allCells = activeTable.querySelectorAll('td, th');
          allCells.forEach(c => {
            c.style.border = '1px solid #1e293b';
            c.style.boxSizing = 'border-box';
          });
        }
        resizingType = null;
        activeCell = null;
        activeRow = null;
        activeTable = null;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    });
  }

  window.switchExcelSheet = function(idxStr) {
    syncActiveExcelDomToState();
    officeEditorState.activeSheetIdx = Math.max(0, parseInt(idxStr, 10) || 0);
    renderExcelGridInModal();
  };

  window.addExcelRow = function() {
    syncActiveExcelDomToState();
    const s = officeEditorState.sheets[officeEditorState.activeSheetIdx];
    if (!s) return;
    const cols = s.rows && s.rows[0] ? s.rows[0].length : 3;
    s.rows.push(new Array(cols).fill(''));
    renderExcelGridInModal();
  };

  window.addExcelCol = function() {
    syncActiveExcelDomToState();
    const s = officeEditorState.sheets[officeEditorState.activeSheetIdx];
    if (!s || !Array.isArray(s.rows)) return;
    s.rows.forEach(r => r.push(''));
    renderExcelGridInModal();
  };

  window.deleteLastExcelRow = function() {
    syncActiveExcelDomToState();
    const s = officeEditorState.sheets[officeEditorState.activeSheetIdx];
    if (!s || !Array.isArray(s.rows) || s.rows.length <= 1) return;
    s.rows.pop();
    renderExcelGridInModal();
  };

  window.saveOfficeEditedDocument = async function() {
    if (!uploadedFile) return;
    const btn = $('btnSaveOfficeEdit');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'সেভ ও প্রিভিউ তৈরি হচ্ছে...';
    }
    try {
      const orientSel = $('officeEditOrientation');
      const chosenOrient = orientSel ? orientSel.value : 'portrait';
      if (officeEditorState.doc_kind === 'excel') {
        syncActiveExcelDomToState();
      } else {
        const wordArea = $('wordEditorArea');
        if (wordArea) {
          const allTables = wordArea.querySelectorAll('table');
          allTables.forEach(tbl => {
            tbl.style.borderCollapse = 'collapse';
            tbl.style.maxWidth = '100%';
            const cells = tbl.querySelectorAll('td, th');
            cells.forEach(c => {
              c.style.border = '1px solid #1e293b';
              c.style.boxSizing = 'border-box';
            });
          });
          officeEditorState.html = wordArea.innerHTML;
        }
      }

      const res = await fetch('/api/office/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_id: uploadedFile.file_id,
          doc_kind: officeEditorState.doc_kind,
          title: officeEditorState.title,
          orientation: chosenOrient,
          margins: officeEditorState.margins || [36.0, 36.0, 36.0, 36.0],
          html: officeEditorState.html,
          sheets: officeEditorState.sheets
        })
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        alert(data.detail || 'ডকুমেন্ট সেভ ব্যর্থ হয়েছে');
        return;
      }

      uploadedFile.page_count = data.page_count || uploadedFile.page_count;
      uploadedFile.office_orientation = chosenOrient;
      selectRadio(chosenOrient === 'landscape' ? 'optLandscape' : 'optPortrait');
      $('fileMetaText').innerText = `মোট পেজ: ${bn(uploadedFile.page_count)} টি (এডিট করা হয়েছে)`;
      bumpFileRev();
      closeOfficeEditModal();
      calculateBill();
      refreshTopLivePreview(true);
    } catch (e) {
      alert('সার্ভার এরর: ' + e);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>✓</span> <span>পরিবর্তন সেভ করুন</span>';
      }
    }
  };

  // ── Direct remove.bg Website Bridge Modal Logic ──
  let rbgNidSide = 'front';

  window.switchNidRbgSide = function(side) {
    rbgNidSide = side === 'back' ? 'back' : 'front';
    const bf = $('btnRbgSideFront');
    const bb = $('btnRbgSideBack');
    if (bf && bb) {
      bf.className = rbgNidSide === 'front'
        ? 'py-1.5 rounded-lg font-bold bg-indigo-600 text-white shadow-sm'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200';
      bb.className = rbgNidSide === 'back'
        ? 'py-1.5 rounded-lg font-bold bg-indigo-600 text-white shadow-sm'
        : 'py-1.5 rounded-lg font-bold text-gray-700 hover:bg-gray-200';
    }
  };

  window.openRemoveBgModal = function() {
    if (!uploadedFile || uploadedFile.file_type !== 'image') return;
    const modal = $('removeBgModal');
    const nidSwitch = $('nidRbgSideSwitch');
    if (nidSwitch) nidSwitch.classList.toggle('hidden', !uploadedFile.is_nid);
    if (modal) modal.classList.remove('hidden');
  };

  window.closeRemoveBgModal = function() {
    const modal = $('removeBgModal');
    if (modal) modal.classList.add('hidden');
  };

  const cutoutInput = $('cutoutFileInput');
  if (cutoutInput) {
    cutoutInput.addEventListener('change', async (e) => {
      const f = e.target.files[0];
      if (!f || !uploadedFile) return;
      const fd = new FormData();
      fd.append('file_id', uploadedFile.file_id);
      fd.append('side', rbgNidSide);
      fd.append('file', f);
      fd.append('bg_color', '#ffffff');

      try {
        const res = await fetch('/api/image/replace-cutout', {
          method: 'POST',
          body: fd
        });
        const data = await res.json();
        if (data.success) {
          bumpFileRev();
          syncActiveStudioPhotoUrl();
          closeRemoveBgModal();
          refreshTopLivePreview(true);
          if (currentMode === 'studio') scheduleStudio();
          alert('✓ remove.bg থেকে ডাউনলোড করা ছবি সফলভাবে যুক্ত হয়েছে!');
        } else {
          alert('আপলোড ব্যর্থ হয়েছে: ' + (data.detail || ''));
        }
      } catch (err) {
        alert('সার্ভার এরর: ' + err);
      } finally {
        cutoutInput.value = '';
      }
    });
  }

  // ── Button triggers for header bar ──
  const btnCropTrigger = $('btnOpenCrop');
  if (btnCropTrigger) btnCropTrigger.addEventListener('click', () => window.openCropModal('front'));
  const btnCamTrigger = $('btnOpenCamScan');
  if (btnCamTrigger) btnCamTrigger.addEventListener('click', window.openCamscanModal);
  const btnRbgTrigger = $('btnOpenRemoveBg');
  if (btnRbgTrigger) btnRbgTrigger.addEventListener('click', window.openRemoveBgModal);

  // ── Rotate File Logic (Image / NID / PDF 90° clockwise) ──
  async function rotateFile(showAlert = true) {
    if (!uploadedFile) return;
    const btn = $('btnRotateFile');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/file/rotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_id: uploadedFile.file_id })
      });
      const data = await res.json();
      if (data.success) {
        bumpFileRev();
        refreshTopLivePreview(true);
        if (currentMode === 'studio') {
          scheduleStudio();
        }
        if (showAlert) alert('✓ ফাইল সফলভাবে ৯০° ঘোরানো হয়েছে!');
      } else {
        if (showAlert) alert('ঘোরানো ব্যর্থ হয়েছে: ' + (data.detail || ''));
      }
    } catch (e) {
      if (showAlert) alert('সার্ভার এরর: ' + e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  const btnRotTrigger = $('btnRotateFile');
  if (btnRotTrigger) btnRotTrigger.addEventListener('click', () => rotateFile(true));

  // ── Print Preview toolbar listeners ──
  const btnPrevRot = $('btnPreviewRotate');
  if (btnPrevRot) {
    btnPrevRot.addEventListener('click', async () => {
      btnPrevRot.disabled = true;
      await rotateFile(false);
      btnPrevRot.disabled = false;
      openPrintPreview();
    });
  }

  const btnPrevToggle = $('btnPreviewToggleOrient');
  if (btnPrevToggle) {
    btnPrevToggle.addEventListener('click', () => {
      const cur = checked('orientation') || 'portrait';
      const target = cur === 'landscape' ? 'portrait' : 'landscape';
      selectRadio(target === 'landscape' ? 'optLandscape' : 'optPortrait');
      refreshTopLivePreview();
      openPrintPreview();
    });
  }

  // ── Image Position & Scale Drag-and-Drop Management ──
  let imagePositionState = {
    pos_x: 0.5,
    pos_y: 0.5,
    scale: 1.0,
    fit_mode: 'contain'
  };

  async function loadImagePositionConfig(fileId) {
    try {
      const res = await fetch(`/api/image-position/get/${encodeURIComponent(fileId)}?t=${Date.now()}`);
      if (res.ok) {
        const data = await res.json();
        if (data.config) {
          imagePositionState = {
            pos_x: typeof data.config.pos_x === 'number' ? data.config.pos_x : 0.5,
            pos_y: typeof data.config.pos_y === 'number' ? data.config.pos_y : 0.5,
            scale: typeof data.config.scale === 'number' ? data.config.scale : 1.0,
            fit_mode: data.config.fit_mode || 'contain'
          };
        }
      }
    } catch (_) {}
  }

  window.openImagePositionModal = function() {
    if (!uploadedFile || uploadedFile.file_type !== 'image') return;
    const modal = $('imagePositionModal');
    if (!modal) return;
    modal.classList.remove('hidden');

    const orient = checked('orientation') || 'portrait';
    const canvas = $('imgPosPaperCanvas');
    if (canvas) {
      if (orient === 'landscape') {
        canvas.className = 'w-[280px] sm:w-[320px] aspect-[297/210] bg-white rounded shadow-lg border-2 border-violet-400 p-2 relative overflow-hidden transition-all select-none cursor-crosshair';
      } else {
        canvas.className = 'w-[220px] sm:w-[250px] aspect-[210/297] bg-white rounded shadow-lg border-2 border-violet-400 p-2 relative overflow-hidden transition-all select-none cursor-crosshair';
      }
    }

    const imgEl = $('imgPosDraggableImg');
    const box = $('imgPosDraggableBox');
    if (imgEl && box) {
      const srcUrl = instantLocalUrl || `/api/preview/${encodeURIComponent(uploadedFile.file_id)}?w=600&v=${fileRev}`;
      imgEl.src = srcUrl;
      imgEl.onload = function() {
        updateModalBoxFromState();
        initInteractiveCanvasDrag();
      };
      if (imgEl.complete && imgEl.naturalWidth) {
        updateModalBoxFromState();
        initInteractiveCanvasDrag();
      }
    }
  };

  window.closeImagePositionModal = function() {
    const modal = $('imagePositionModal');
    if (modal) modal.classList.add('hidden');
  };

  function updateModalBoxFromState() {
    const canvas = $('imgPosPaperCanvas');
    const box = $('imgPosDraggableBox');
    const imgEl = $('imgPosDraggableImg');
    if (!canvas || !box || !imgEl) return;

    const canvasW = canvas.clientWidth || 240;
    const canvasH = canvas.clientHeight || 340;
    const margin = 10;
    const availW = Math.max(20, canvasW - 2 * margin);
    const availH = Math.max(20, canvasH - 2 * margin);

    const nw = imgEl.naturalWidth || 300;
    const nh = imgEl.naturalHeight || 200;
    const imgAspect = nw / nh;
    const availAspect = availW / availH;

    let baseW, baseH;
    if (imagePositionState.fit_mode === 'actual') {
      const pxRatio = canvasW / 827;
      baseW = Math.min(availW, Math.max(30, nw * pxRatio));
      baseH = Math.min(availH, Math.max(30, nh * pxRatio));
    } else {
      if (imgAspect > availAspect) {
        baseW = availW;
        baseH = availW / imgAspect;
      } else {
        baseH = availH;
        baseW = availH * imgAspect;
      }
    }

    const boxW = Math.max(24, Math.round(baseW * imagePositionState.scale));
    const boxH = Math.max(24, Math.round(baseH * imagePositionState.scale));

    box.style.width = boxW + 'px';
    box.style.height = boxH + 'px';

    const maxLeft = Math.max(0, canvasW - 2 * margin - boxW);
    const maxTop = Math.max(0, canvasH - 2 * margin - boxH);

    const left = margin + Math.round(maxLeft * imagePositionState.pos_x);
    const top = margin + Math.round(maxTop * imagePositionState.pos_y);

    box.style.left = left + 'px';
    box.style.top = top + 'px';

    const pctX = Math.round(imagePositionState.pos_x * 100);
    const pctY = Math.round(imagePositionState.pos_y * 100);
    const coordsText = $('imgPosCoordsText');
    if (coordsText) {
      coordsText.innerText = `অবস্থান: ডানে ${pctX}%, নিচে ${pctY}%`;
    }

    const slider = $('imgPosScaleSlider');
    const scaleText = $('imgPosScaleText');
    if (slider) slider.value = Math.round(imagePositionState.scale * 100);
    if (scaleText) {
      if (imagePositionState.fit_mode === 'actual') {
        scaleText.innerText = '📷 আসল সাইজ (Actual Size)';
      } else {
        scaleText.innerText = `${Math.round(imagePositionState.scale * 100)}% সাইজ`;
      }
    }
  }

  let canvasDragInitialized = false;
  function initInteractiveCanvasDrag() {
    const canvas = $('imgPosPaperCanvas');
    const box = $('imgPosDraggableBox');
    if (!canvas || !box || canvasDragInitialized) return;
    canvasDragInitialized = true;

    let isDragging = false;
    let startX = 0, startY = 0;
    let startLeft = 0, startTop = 0;

    function onPointerDown(e) {
      e.preventDefault();
      isDragging = true;
      try { box.setPointerCapture(e.pointerId); } catch (_) {}
      startX = e.clientX;
      startY = e.clientY;
      startLeft = box.offsetLeft;
      startTop = box.offsetTop;
      box.classList.add('cursor-grabbing');
    }

    function onPointerMove(e) {
      if (!isDragging) return;
      e.preventDefault();
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      const canvasW = canvas.clientWidth || 240;
      const canvasH = canvas.clientHeight || 340;
      const margin = 10;
      const boxW = box.offsetWidth;
      const boxH = box.offsetHeight;

      const maxLeft = Math.max(0, canvasW - 2 * margin - boxW);
      const maxTop = Math.max(0, canvasH - 2 * margin - boxH);

      let newLeft = Math.max(margin, Math.min(margin + maxLeft, startLeft + dx));
      let newTop = Math.max(margin, Math.min(margin + maxTop, startTop + dy));

      box.style.left = newLeft + 'px';
      box.style.top = newTop + 'px';

      imagePositionState.pos_x = maxLeft > 0 ? (newLeft - margin) / maxLeft : 0.5;
      imagePositionState.pos_y = maxTop > 0 ? (newTop - margin) / maxTop : 0.5;

      const pctX = Math.round(imagePositionState.pos_x * 100);
      const pctY = Math.round(imagePositionState.pos_y * 100);
      const coordsText = $('imgPosCoordsText');
      if (coordsText) {
        coordsText.innerText = `অবস্থান: ডানে ${pctX}%, নিচে ${pctY}%`;
      }
    }

    function onPointerUp(e) {
      if (!isDragging) return;
      isDragging = false;
      try { box.releasePointerCapture(e.pointerId); } catch (_) {}
      box.classList.remove('cursor-grabbing');
    }

    box.addEventListener('pointerdown', onPointerDown);
    box.addEventListener('pointermove', onPointerMove);
    box.addEventListener('pointerup', onPointerUp);
    box.addEventListener('pointercancel', onPointerUp);
  }

  window.nudgeImagePosition = function(dx, dy) {
    imagePositionState.pos_x = Math.max(0, Math.min(1, imagePositionState.pos_x + dx));
    imagePositionState.pos_y = Math.max(0, Math.min(1, imagePositionState.pos_y + dy));
    updateModalBoxFromState();
    const modal = $('imagePositionModal');
    if (!modal || modal.classList.contains('hidden')) {
      saveImagePositionAndApply(false);
    }
  };

  window.setImagePositionPreset = function(x, y, scale = null) {
    imagePositionState.pos_x = x;
    imagePositionState.pos_y = y;
    if (scale !== null) {
      imagePositionState.scale = scale;
      imagePositionState.fit_mode = 'contain';
    }
    updateModalBoxFromState();
    const modal = $('imagePositionModal');
    if (!modal || modal.classList.contains('hidden')) {
      saveImagePositionAndApply(false);
    }
  };

  window.onImageScaleSliderChange = function(val) {
    imagePositionState.scale = Math.max(0.1, Math.min(1.0, parseFloat(val) / 100));
    imagePositionState.fit_mode = 'contain';
    updateModalBoxFromState();
  };

  window.setImageScalePreset = function(sc) {
    imagePositionState.scale = sc;
    imagePositionState.fit_mode = 'contain';
    updateModalBoxFromState();
  };

  window.setImageActualSizePreset = function() {
    imagePositionState.fit_mode = 'actual';
    imagePositionState.scale = 1.0;
    updateModalBoxFromState();
  };

  window.saveImagePositionAndApply = async function(showFeedback = true) {
    if (!uploadedFile) return;
    const btn = $('btnSaveImagePosition');
    if (btn && showFeedback) {
      btn.disabled = true;
      btn.innerText = 'সেভ হচ্ছে...';
    }
    try {
      await fetch('/api/image-position/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_id: uploadedFile.file_id,
          pos_x: imagePositionState.pos_x,
          pos_y: imagePositionState.pos_y,
          scale: imagePositionState.scale,
          fit_mode: imagePositionState.fit_mode
        })
      });

      uploadedFile.image_pos = { ...imagePositionState };
      bumpFileRev();
      closeImagePositionModal();
      refreshTopLivePreview(true);
    } catch (e) {
      if (showFeedback) alert('পজিশন সেভ ব্যর্থ হয়েছে: ' + e);
    } finally {
      if (btn && showFeedback) {
        btn.disabled = false;
        btn.innerHTML = '<span>✅</span> <span>ঠিক আছে (OK / পজিশন সেট করুন)</span>';
      }
    }
  };

  // Direct Drag on Main Live Preview Sheet for Images
  (function initSheetLiveDrag() {
    const sheet = $('docPaperSheet');
    if (!sheet) return;
    let isDragging = false;
    let startX = 0, startY = 0;
    let origPosX = 0.5, origPosY = 0.5;

    sheet.addEventListener('pointerdown', function(e) {
      if (!uploadedFile || uploadedFile.file_type !== 'image' || currentMode === 'studio') return;
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      origPosX = imagePositionState.pos_x;
      origPosY = imagePositionState.pos_y;
      sheet.style.cursor = 'grabbing';
      try { sheet.setPointerCapture(e.pointerId); } catch (_) {}
    });

    sheet.addEventListener('pointermove', function(e) {
      if (!isDragging) return;
      const dx = (e.clientX - startX) / (sheet.clientWidth || 240);
      const dy = (e.clientY - startY) / (sheet.clientHeight || 340);
      imagePositionState.pos_x = Math.max(0, Math.min(1, origPosX + dx));
      imagePositionState.pos_y = Math.max(0, Math.min(1, origPosY + dy));
    });

    sheet.addEventListener('pointerup', function(e) {
      if (!isDragging) return;
      isDragging = false;
      sheet.style.cursor = '';
      try { sheet.releasePointerCapture(e.pointerId); } catch (_) {}
      saveImagePositionAndApply(false);
    });
  })();

  // Restore previously uploaded file across page refresh
  restoreSessionState();

  // Initialize Recent Prints panel & live status sync on load
  renderRecentOrdersPanel();
  syncRecentOrdersStatus().then(() => {
    const hasActive = loadRecentOrders().some((x) => x && (x.status === 'pending_payment' || x.status === 'paid' || x.status === 'printing'));
    if (hasActive && $('recentPrintsPanel')) {
      $('recentPrintsPanel').classList.remove('hidden');
    }
  });

})();
