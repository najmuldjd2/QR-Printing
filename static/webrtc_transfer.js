// Universal WebRTC Direct Peer-to-Peer High-Speed Transfer Engine
// Architecture: Pipelined Double-Buffered Disk Streaming + 64KB SCTP Framing + Low-Latency Network Flow Control
(function() {
  const CHUNK_SIZE = 64 * 1024;             // 64 KB chunks
  const BLOCK_SIZE = 4 * 1024 * 1024;       // 4 MB blocks (pipelined prefetch from disk)
  const BUFFER_LOW_THRESHOLD = 512 * 1024;  // 512 KB
  const BUFFER_MAX = 2 * 1024 * 1024;       // 2 MB network buffer ceiling (tuned for 5-15 MB/s)
  const BATCH_FLUSH_BYTES = 16 * 1024 * 1024;// 16 MB Blob parts for receiver memory safety

  const ICE_SERVERS = [
    { urls: 'stun:stun.cloudflare.com:3478' },
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'stun:stun1.l.google.com:19302' },
    { urls: 'stun:stun2.l.google.com:19302' }
  ];

  const myPeerId = Math.random().toString(36).slice(2, 10) + '_' + Date.now().toString(36).slice(-4);
  let myDevice = 'ডিভাইস';
  let remoteDevice = 'অন্য ডিভাইস';
  let pc = null;
  let dc = null;
  let ws = null;
  let peerReady = false;
  let makingOffer = false;
  let pendingCandidates = [];
  let statsInterval = null;

  // Receiver state
  let incomingInfo = null;
  let receivedBlobs = [];
  let currentBufferBatch = [];
  let currentBatchBytes = 0;
  let receivedBytes = 0;
  let recvStartTime = 0;
  let lastRecvUiTime = 0;

  window.isP2PReady = function() {
    return peerReady && dc && dc.readyState === 'open';
  };

  window.initWebRTC = function(websocketInstance, deviceLabel) {
    ws = websocketInstance;
    if (deviceLabel && typeof deviceLabel === 'string') {
      myDevice = deviceLabel;
    }
    setTimeout(() => {
      sendSignal({ type: 'webrtc_join', peerId: myPeerId, device: myDevice });
    }, 250);
  };

  function cleanupPeerConnection() {
    peerReady = false;
    pendingCandidates = [];
    if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }
    if (dc) {
      try { dc.close(); } catch (e) {}
      dc = null;
    }
    if (pc) {
      try { pc.close(); } catch (e) {}
      pc = null;
    }
  }

  function createPeerConnection(asInitiator) {
    cleanupPeerConnection();
    try {
      pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });

      pc.onicecandidate = (e) => {
        if (e.candidate) {
          sendSignal({
            type: 'webrtc_candidate',
            peerId: myPeerId,
            candidate: e.candidate
          });
        }
      };

      pc.onconnectionstatechange = () => {
        if (!pc) return;
        if (pc.connectionState === 'connected') {
          peerReady = true;
          updateP2PStatus('connected');
          checkConnectionQuality();
          if (!statsInterval) statsInterval = setInterval(checkConnectionQuality, 2500);
        } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected' || pc.connectionState === 'closed') {
          peerReady = false;
          if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }
          updateP2PStatus('disconnected');
        }
      };

      if (asInitiator) {
        dc = pc.createDataChannel('fileTransfer', { ordered: true });
        setupDataChannel(dc);
      } else {
        pc.ondatachannel = (e) => {
          dc = e.channel;
          setupDataChannel(dc);
        };
      }
    } catch (e) {
      console.log('PeerConnection creation failed:', e);
    }
  }

  async function flushPendingCandidates() {
    if (!pc || !pc.remoteDescription) return;
    while (pendingCandidates.length > 0) {
      const c = pendingCandidates.shift();
      try {
        await pc.addIceCandidate(new RTCIceCandidate(c));
      } catch (e) {}
    }
  }

  async function initiateOffer() {
    try {
      makingOffer = true;
      createPeerConnection(true);
      if (!pc) return;
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      sendSignal({
        type: 'webrtc_offer',
        peerId: myPeerId,
        device: myDevice,
        sdp: pc.localDescription
      });
    } catch (e) {
      console.log('Offer creation error:', e);
    } finally {
      makingOffer = false;
    }
  }

  window.handleWebRTCSignal = async function(msg) {
    if (!msg || msg.peerId === myPeerId) return;
    if (msg.device) remoteDevice = msg.device;

    try {
      if (msg.type === 'webrtc_join') {
        await initiateOffer();
      } else if (msg.type === 'webrtc_offer') {
        const polite = myPeerId < (msg.peerId || '');
        const offerCollision = makingOffer || (pc && pc.signalingState !== 'stable');
        if (offerCollision && !polite) {
          return;
        }
        createPeerConnection(false);
        if (!pc) return;
        await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
        await flushPendingCandidates();
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        sendSignal({
          type: 'webrtc_answer',
          peerId: myPeerId,
          device: myDevice,
          sdp: pc.localDescription
        });
      } else if (msg.type === 'webrtc_answer') {
        if (!pc) return;
        if (pc.signalingState === 'have-local-offer') {
          await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
          await flushPendingCandidates();
        }
      } else if (msg.type === 'webrtc_candidate') {
        if (!msg.candidate) return;
        if (pc && pc.remoteDescription && pc.remoteDescription.type) {
          await pc.addIceCandidate(new RTCIceCandidate(msg.candidate));
        } else {
          pendingCandidates.push(msg.candidate);
        }
      }
    } catch (err) {
      console.log('[WebRTC Signal Error]', err);
    }
  };

  function sendSignal(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(obj)); } catch (e) {}
    }
  }

  function setupDataChannel(channel) {
    channel.binaryType = 'arraybuffer';
    try {
      channel.bufferedAmountLowThreshold = BUFFER_LOW_THRESHOLD;
    } catch (e) {}

    channel.onopen = () => {
      peerReady = true;
      updateP2PStatus('connected');
      checkConnectionQuality();
    };
    channel.onclose = () => {
      peerReady = false;
      updateP2PStatus('disconnected');
    };
    channel.onmessage = (e) => {
      handleChannelMessage(e.data);
    };
  }

  async function checkConnectionQuality() {
    if (!pc) return;
    try {
      const stats = await pc.getStats();
      let rttMs = null;
      let localCandidateType = '';
      for (const report of stats.values()) {
        if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          if (report.currentRoundTripTime !== undefined) {
            rttMs = Math.round(report.currentRoundTripTime * 1000);
          }
          const local = stats.get(report.localCandidateId);
          if (local && local.candidateType) {
            localCandidateType = local.candidateType;
          }
        }
      }

      const badge = document.getElementById('p2pStatusBadge');
      if (badge && peerReady) {
        if (localCandidateType === 'host' || (rttMs !== null && rttMs < 25)) {
          badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-green-100 text-green-800 flex items-center gap-1 cursor-pointer';
          badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span> ⚡ ডিরেক্ট লোকাল ওয়াইফাই (${rttMs !== null ? rttMs + 'ms' : 'LAN'})`;
          badge.onclick = null;
        } else {
          badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-blue-100 text-blue-800 flex items-center gap-1 cursor-pointer hover:bg-blue-200';
          badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-blue-500 animate-pulse"></span> 🌐 ইন্টারনেট P2P (${rttMs !== null ? rttMs + 'ms' : 'STUN'}) • ⚡ স্পিড বাড়ান`;
          badge.onclick = () => window.showP2PHelpModal(true);
        }
      }
    } catch (e) {}
  }

  function updateP2PStatus(state) {
    const badge = document.getElementById('p2pStatusBadge');
    if (!badge) return;
    if (state === 'connected') {
      badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-green-100 text-green-800 flex items-center gap-1 cursor-pointer';
      badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span> ⚡ ডিরেক্ট সংযুক্ত (${esc(myDevice)} ⇄ ${esc(remoteDevice)})`;
      badge.onclick = null;
    } else {
      badge.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-amber-100 text-amber-800 flex items-center gap-1 cursor-pointer hover:bg-amber-200';
      badge.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-500"></span> ☁️ ক্লাউড ও P2P রেডি (৫০ MB+) • ❓ সাহায্য';
      badge.onclick = () => window.showP2PHelpModal(false);
    }
  }

  window.showP2PHelpModal = function(isOverInternet) {
    let m = document.getElementById('p2pHelpModal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'p2pHelpModal';
      m.className = 'fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4';
      document.body.appendChild(m);
    }
    m.classList.remove('hidden');

    const warningHtml = isOverInternet ? `
      <div class="bg-amber-50 border border-amber-300 p-3 rounded-xl text-amber-900 space-y-1">
        <p class="font-bold flex items-center gap-1"><span>⚠️</span> <span>আপনার ডিভাইস দুটি বর্তমানে ইন্টারনেটের মাধ্যমে যুক্ত রয়েছে!</span></p>
        <p class="text-[11px]">একটি ডিভাইস হয়তো মোবাইল ডাটায় (SIM) আছে অথবা ওয়াইফাই রাউটার লোকাল কানেকশন ব্লক করছে। ইন্টারনেট দিয়ে ফাইল গেলে মোবাইল ডাটার স্পিডে যাবে।</p>
      </div>
    ` : '';

    m.innerHTML = `
      <div class="bg-white max-w-md w-full rounded-2xl p-5 shadow-2xl border space-y-3.5 text-xs">
        <div class="flex items-center justify-between border-b pb-2.5">
          <h3 class="font-bold text-sm text-gray-900 flex items-center gap-1.5">
            <span>⚡</span> <span>সর্বোচ্চ ওয়াইফাই স্পিড (২০-৪০ MB/s) পাওয়ার নিয়ম</span>
          </h3>
          <button onclick="document.getElementById('p2pHelpModal').classList.add('hidden')" class="text-gray-400 hover:text-gray-600 text-2xl leading-none">&times;</button>
        </div>
        ${warningHtml}
        <div class="space-y-2 bg-gray-50 p-3 rounded-xl border">
          <p class="font-bold text-gray-800">রকেট স্পিড পেতে নিচের যেকোনো ১টি করুন:</p>
          <ul class="list-disc list-inside space-y-1.5 text-gray-700">
            <li><b>উভয় ডিভাইস একই ওয়াইফাইতে রাখুন:</b> মোবাইল ও কম্পিউটার দুটোই একই ওয়াইফাই রাউটারে কানেক্ট থাকলে সরাসরি লোকাল লিংকে সর্বোচ্চ স্পিডে যাবে।</li>
            <li><b>হটস্পট ৫.০ GHz করুন:</b> মোবাইলের Hotspot সেটিংসে গিয়ে <b>AP Band (ব্যান্ড)</b> ২.৪ GHz থেকে <b>৫.০ GHz</b> সিলেক্ট করুন। ২.৪ গিগাহার্জে সর্বোচ্চ ১-১.৫ MB/s ওঠে, কিন্তু ৫ গিগাহার্জ দিলে গতি <b>৫ থেকে ১৫+ MB/s</b> হয়ে যাবে!</li>
            <li><b>মোবাইল ডাটা (SIM) অফ রাখুন:</b> ফাইল ট্রান্সফার করার সময় মোবাইল ডাটা অফ রেখে ওয়াইফাইতে থাকলে ইন্টারনেট ট্রাফিকের স্পিড লিমিটেশন থাকে না।</li>
          </ul>
        </div>
        <button onclick="document.getElementById('p2pHelpModal').classList.add('hidden')" class="w-full py-2.5 bg-gray-900 hover:bg-black text-white font-bold rounded-xl text-center transition">
          বুঝেছি, ঠিক আছে
        </button>
      </div>
    `;
  };

  // High-Speed Pipelined Sender Engine
  window.sendP2PFile = function(file, onProgress) {
    if (!dc || dc.readyState !== 'open') return false;

    try {
      dc.bufferedAmountLowThreshold = BUFFER_LOW_THRESHOLD;
    } catch (e) {}

    dc.send(JSON.stringify({
      meta: true,
      name: file.name,
      size: file.size,
      type: file.type,
      sender: myDevice
    }));

    let fileOffset = 0;          // bytes read from disk so far
    let sentOffset = 0;          // bytes queued to dc.send so far
    let activeBlock = null;      // ArrayBuffer in RAM being sent
    let activeBlockOffset = 0;   // offset within activeBlock
    let nextBlock = null;        // pre-fetched next ArrayBuffer from disk
    let isReadingNext = false;   // disk read in progress
    let isFinished = false;
    let isPausedForDisk = false;
    let watchdogTimer = null;
    const startTime = Date.now();
    let lastProgressTime = 0;

    function report(force) {
      const now = Date.now();
      if (!force && (now - lastProgressTime < 100) && sentOffset < file.size) return;
      lastProgressTime = now;
      const elapsed = (now - startTime) / 1000;
      const speedBps = elapsed > 0.1 ? sentOffset / elapsed : 0;
      if (onProgress) onProgress(sentOffset, file.size, speedBps);
    }

    function prefetchNextBlock() {
      if (isReadingNext || nextBlock || fileOffset >= file.size) return;
      isReadingNext = true;
      const start = fileOffset;
      const end = Math.min(file.size, fileOffset + BLOCK_SIZE);
      fileOffset = end;

      const reader = new FileReader();
      reader.onload = (e) => {
        isReadingNext = false;
        nextBlock = e.target.result;
        if (isPausedForDisk) {
          isPausedForDisk = false;
          pump();
        }
      };
      reader.onerror = () => {
        isReadingNext = false;
        console.error('P2P FileReader error on slice');
      };
      reader.readAsArrayBuffer(file.slice(start, end));
    }

    function scheduleWatchdog() {
      if (watchdogTimer) return;
      watchdogTimer = setTimeout(() => {
        watchdogTimer = null;
        pump();
      }, 10);
    }

    function pump() {
      if (isFinished || !dc || dc.readyState !== 'open') return;

      while (true) {
        // Need to load or switch to next block from memory
        if (!activeBlock || activeBlockOffset >= activeBlock.byteLength) {
          if (nextBlock) {
            activeBlock = nextBlock;
            activeBlockOffset = 0;
            nextBlock = null;
            prefetchNextBlock(); // Start prefetching the following block in the background
          } else if (sentOffset >= file.size) {
            // All file bytes queued
            if (dc.bufferedAmount === 0) {
              isFinished = true;
              report(true);
              if (onProgress) onProgress(file.size, file.size, 0);
              return;
            } else {
              scheduleWatchdog();
              return;
            }
          } else {
            // Waiting for disk prefetch
            isPausedForDisk = true;
            prefetchNextBlock();
            return;
          }
        }

        // Check if network socket buffer is full
        if (dc.bufferedAmount >= BUFFER_MAX) {
          scheduleWatchdog();
          return;
        }

        // Send next chunk from RAM (Zero-Copy TypedArray view)
        const len = Math.min(CHUNK_SIZE, activeBlock.byteLength - activeBlockOffset);
        const chunkView = new Uint8Array(activeBlock, activeBlockOffset, len);
        try {
          dc.send(chunkView);
        } catch (err) {
          console.warn('dc.send full:', err);
          scheduleWatchdog();
          return;
        }

        activeBlockOffset += len;
        sentOffset += len;
        report(false);
      }
    }

    dc.onbufferedamountlow = () => {
      pump();
    };

    // Preload block 0 and block 1, then start pump
    prefetchNextBlock();
    pump();
    return true;
  };

  // High-Speed receiver engine with 16MB Blob part aggregation (memory-safe for 1GB+)
  function handleChannelMessage(data) {
    if (typeof data === 'string') {
      try {
        const msg = JSON.parse(data);
        if (msg.meta) {
          incomingInfo = msg;
          receivedBlobs = [];
          currentBufferBatch = [];
          currentBatchBytes = 0;
          receivedBytes = 0;
          recvStartTime = Date.now();
          lastRecvUiTime = 0;
          showP2PDownloadProgress(msg.name, msg.size, 0, 0);
        }
      } catch (e) {}
    } else {
      currentBufferBatch.push(data);
      currentBatchBytes += data.byteLength;
      receivedBytes += data.byteLength;

      // Offload 16MB batches into Blob parts to keep JS heap small
      if (currentBatchBytes >= BATCH_FLUSH_BYTES) {
        receivedBlobs.push(new Blob(currentBufferBatch));
        currentBufferBatch = [];
        currentBatchBytes = 0;
      }

      if (incomingInfo) {
        const now = Date.now();
        if (now - lastRecvUiTime > 100 || receivedBytes >= incomingInfo.size) {
          lastRecvUiTime = now;
          const elapsed = (now - recvStartTime) / 1000;
          const speedBps = elapsed > 0.1 ? receivedBytes / elapsed : 0;
          showP2PDownloadProgress(incomingInfo.name, incomingInfo.size, receivedBytes, speedBps);
        }

        if (receivedBytes >= incomingInfo.size) {
          if (currentBufferBatch.length > 0) {
            receivedBlobs.push(new Blob(currentBufferBatch));
            currentBufferBatch = [];
          }
          const finalBlob = new Blob(receivedBlobs, { type: incomingInfo.type || 'application/octet-stream' });
          triggerBlobDownload(finalBlob, incomingInfo.name);
          addP2PFileToList(incomingInfo.name, incomingInfo.size, finalBlob, incomingInfo.sender);
          incomingInfo = null;
          receivedBlobs = [];
          receivedBytes = 0;
        }
      }
    }
  }

  function showP2PDownloadProgress(name, size, loaded, speedBps) {
    let p = document.getElementById('p2pProgressBox');
    if (!p) {
      p = document.createElement('div');
      p.id = 'p2pProgressBox';
      p.className = 'p-3 bg-pink-50 border-2 border-pink-300 rounded-xl space-y-1 mb-2 text-xs shadow-sm';
      p.innerHTML = `
        <div class="flex justify-between items-center font-bold text-gray-800">
          <span class="p2p-name truncate max-w-[220px]"></span>
          <span class="p2p-pct text-pink-600 font-mono font-bold">0%</span>
        </div>
        <div class="flex justify-between text-[11px] text-gray-600 font-mono pt-0.5">
          <span class="p2p-stats"></span>
          <span class="p2p-speed text-emerald-700 font-extrabold"></span>
        </div>
        <div class="w-full bg-gray-200 h-2.5 rounded-full overflow-hidden mt-1">
          <div class="p2p-bar bg-gradient-to-r from-pink-600 to-orange-500 h-full w-0 transition-all duration-150"></div>
        </div>
      `;
      const container = document.getElementById('fileList');
      if (container) container.parentNode.insertBefore(p, container);
    }

    p.classList.remove('hidden');
    const pct = Math.min(100, Math.round(loaded / size * 100));
    p.querySelector('.p2p-name').textContent = `⚡ ডিরেক্ট আসছে: ${name}`;
    p.querySelector('.p2p-pct').textContent = `${pct}%`;
    p.querySelector('.p2p-stats').textContent = `${fmtSize(loaded)} / ${fmtSize(size)}`;

    if (speedBps > 1024) {
      const remainingBytes = Math.max(0, size - loaded);
      const remainingSec = Math.round(remainingBytes / speedBps);
      const etaStr = remainingSec > 60 ? `${Math.floor(remainingSec / 60)} মি. ${remainingSec % 60} সে.` : `${remainingSec} সে.`;
      p.querySelector('.p2p-speed').textContent = `⚡ ${fmtSpeed(speedBps)} (বাকি: ${etaStr})`;
    } else {
      p.querySelector('.p2p-speed').textContent = `⚡ হিসাব হচ্ছে...`;
    }

    p.querySelector('.p2p-bar').style.width = pct + '%';

    if (pct >= 100) {
      p.querySelector('.p2p-speed').textContent = `✅ ডাউনলোড সম্পন্ন!`;
      p.querySelector('.p2p-bar').classList.replace('from-pink-600', 'from-emerald-500');
      p.querySelector('.p2p-bar').classList.replace('to-orange-500', 'to-emerald-600');
      setTimeout(() => { p.classList.add('hidden'); }, 4000);
    }
  }

  function triggerBlobDownload(blob, filename) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
  }

  function addP2PFileToList(name, size, blob, sender) {
    const list = document.getElementById('fileList');
    if (!list) return;
    const emptyMsg = list.querySelector('p.text-center');
    if (emptyMsg) emptyMsg.remove();

    const url = URL.createObjectURL(blob);
    const item = document.createElement('div');
    item.className = 'border-2 border-green-300 bg-green-50/50 rounded-xl p-3 flex items-center justify-between gap-2 shadow-xs';
    item.innerHTML = `
      <div class="min-w-0">
        <p class="font-bold text-sm text-gray-900 truncate">⚡ ${esc(name)}</p>
        <p class="text-[11px] text-green-700">ডিরেক্ট ডিভাইস ট্রান্সফার (${esc(sender || 'অন্য ডিভাইস')} থেকে) • ${fmtSize(size)}</p>
      </div>
      <a href="${url}" download="${esc(name)}" class="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white rounded-lg text-xs font-bold shrink-0 shadow-sm">⬇️ সেভ করুন</a>
    `;
    list.prepend(item);
  }

  function esc(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmtSize(b) {
    return b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(0) + ' KB' : b < 1073741824 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1073741824).toFixed(2) + ' GB';
  }
  function fmtSpeed(bps) {
    if (bps < 1048576) {
      return (bps / 1024).toFixed(0) + ' KB/s';
    }
    return (bps / 1048576).toFixed(1) + ' MB/s';
  }
  window.fmtSize = fmtSize;
  window.fmtSpeed = fmtSpeed;
})();
