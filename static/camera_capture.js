/* QR Se Print – shared live camera capture (mobile / PC / laptop webcam)
   Usage: QRCamera.open({ title, facing: 'environment'|'user', onCapture(file) })
   Falls back to the native file-input camera (capture attribute) when getUserMedia is unavailable. */
(function () {
  let stream = null;
  let facing = 'environment';
  let devices = [];
  let deviceIdx = -1;
  let opts = null;
  let root = null;

  function el(html) {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstChild;
  }

  function build() {
    if (root) return;
    root = el(`
      <div id="qrCamModal" style="position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.92);display:none;flex-direction:column;align-items:center;justify-content:center;font-family:inherit">
        <div style="width:100%;max-width:720px;display:flex;flex-direction:column;height:100%;max-height:100vh">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;color:#fff">
            <b id="qrCamTitle" style="font-size:15px">📷 ক্যামেরা</b>
            <button type="button" id="qrCamClose" style="color:#fff;font-size:28px;line-height:1;background:none;border:0;cursor:pointer">&times;</button>
          </div>
          <div style="position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;background:#000">
            <video id="qrCamVideo" autoplay playsinline muted style="max-width:100%;max-height:100%;width:100%;height:100%;object-fit:contain"></video>
            <div id="qrCamMsg" style="position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;color:#fff;font-size:13px;padding:0 20px"></div>
            <div id="qrCamFlash" style="position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .25s"></div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-around;padding:14px 10px 20px;gap:8px">
            <button type="button" id="qrCamGallery" style="padding:9px 14px;border-radius:12px;background:#374151;color:#fff;font-weight:700;font-size:12px;border:0;cursor:pointer">📁 গ্যালারি</button>
            <button type="button" id="qrCamShot" aria-label="ছবি তুলুন" style="width:68px;height:68px;border-radius:50%;background:#fff;border:5px solid #ec4899;cursor:pointer"></button>
            <button type="button" id="qrCamSwitch" style="padding:9px 14px;border-radius:12px;background:#374151;color:#fff;font-weight:700;font-size:12px;border:0;cursor:pointer">🔄 ক্যামেরা বদলান</button>
          </div>
        </div>
      </div>`);
    document.body.appendChild(root);

    root.querySelector('#qrCamClose').addEventListener('click', close);
    root.querySelector('#qrCamShot').addEventListener('click', shoot);
    root.querySelector('#qrCamSwitch').addEventListener('click', switchCamera);
    root.querySelector('#qrCamGallery').addEventListener('click', () => {
      const cb = opts && opts.onCapture;
      close();
      pickFromInput(cb, false);
    });
    document.addEventListener('keydown', (e) => {
      if (root.style.display === 'none') return;
      if (e.key === 'Escape') close();
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); shoot(); }
    });
  }

  function pickFromInput(cb, useCamera) {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'image/*';
    if (useCamera) inp.setAttribute('capture', facing === 'user' ? 'user' : 'environment');
    inp.style.display = 'none';
    inp.addEventListener('change', () => {
      const f = inp.files && inp.files[0];
      inp.remove();
      if (f && cb) cb(f);
    });
    document.body.appendChild(inp);
    inp.click();
  }

  function stopStream() {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    const v = root && root.querySelector('#qrCamVideo');
    if (v) v.srcObject = null;
  }

  function msg(t) {
    root.querySelector('#qrCamMsg').innerText = t || '';
  }

  async function start() {
    stopStream();
    msg('ক্যামেরা চালু হচ্ছে...');
    const video = root.querySelector('#qrCamVideo');
    const base = { width: { ideal: 1920 }, height: { ideal: 1080 } };
    const attempts = [];
    if (deviceIdx >= 0 && devices[deviceIdx]) {
      attempts.push({ video: Object.assign({ deviceId: { exact: devices[deviceIdx].deviceId } }, base), audio: false });
    }
    attempts.push({ video: Object.assign({ facingMode: { ideal: facing } }, base), audio: false });
    attempts.push({ video: true, audio: false });

    let lastErr = null;
    for (const c of attempts) {
      try {
        stream = await navigator.mediaDevices.getUserMedia(c);
        break;
      } catch (e) { lastErr = e; }
    }
    if (!stream) {
      throw lastErr || new Error('camera unavailable');
    }
    video.srcObject = stream;
    const settings = (stream.getVideoTracks()[0] || {}).getSettings ? stream.getVideoTracks()[0].getSettings() : {};
    const isFront = settings.facingMode === 'user' || (facing === 'user' && !settings.facingMode);
    video.style.transform = isFront ? 'scaleX(-1)' : 'none';
    try { await video.play(); } catch (e) { /* autoplay handled by attribute */ }
    msg('');
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      devices = all.filter((d) => d.kind === 'videoinput');
      const cur = settings.deviceId;
      const i = devices.findIndex((d) => d.deviceId === cur);
      if (i >= 0) deviceIdx = i;
      root.querySelector('#qrCamSwitch').style.display = devices.length > 1 ? '' : 'none';
    } catch (e) { /* ignore */ }
  }

  function switchCamera() {
    if (devices.length > 1) {
      deviceIdx = (deviceIdx + 1) % devices.length;
    }
    facing = facing === 'user' ? 'environment' : 'user';
    start().catch(onStartFail);
  }

  function onStartFail(e) {
    const denied = e && (e.name === 'NotAllowedError' || e.name === 'SecurityError');
    msg(denied
      ? 'ক্যামেরার অনুমতি দেওয়া হয়নি। ব্রাউজারের অ্যাড্রেস বারে ক্যামেরা অনুমতি দিন, অথবা নিচের "গ্যালারি" বাটন ব্যবহার করুন।'
      : 'ক্যামেরা চালু করা যায়নি। অন্য অ্যাপ ক্যামেরা ব্যবহার করছে কিনা দেখুন, অথবা "গ্যালারি" বাটন ব্যবহার করুন।');
  }

  function shoot() {
    const video = root.querySelector('#qrCamVideo');
    if (!stream || !video.videoWidth) return;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    if (video.style.transform === 'scaleX(-1)') {
      ctx.translate(canvas.width, 0);
      ctx.scale(-1, 1);
    }
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const flash = root.querySelector('#qrCamFlash');
    flash.style.opacity = '.85';
    setTimeout(() => { flash.style.opacity = '0'; }, 120);
    const cb = opts && opts.onCapture;
    canvas.toBlob((blob) => {
      if (!blob) return;
      const file = new File([blob], 'camera_' + Date.now() + '.jpg', { type: 'image/jpeg' });
      close();
      if (cb) cb(file);
    }, 'image/jpeg', 0.92);
  }

  function close() {
    stopStream();
    if (root) root.style.display = 'none';
  }

  async function open(o) {
    opts = o || {};
    facing = opts.facing === 'user' ? 'user' : 'environment';
    deviceIdx = -1;
    const supported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && window.isSecureContext !== false;
    if (!supported) {
      pickFromInput(opts.onCapture, true);
      return;
    }
    build();
    root.querySelector('#qrCamTitle').innerText = opts.title || '📷 ক্যামেরা দিয়ে ছবি তুলুন';
    root.style.display = 'flex';
    try {
      await start();
    } catch (e) {
      onStartFail(e);
    }
  }

  window.QRCamera = { open, close };
})();
