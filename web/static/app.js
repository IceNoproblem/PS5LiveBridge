/* PS5 直播桥 · 前端
   界面只渲染 /api/state 的结果，不自己推断状态。
   五个步骤：配路由器 / 检测 / 填 PS5 / 登录 / 开播。 */

const S = {
  step: 1,
  state: null,
  env: null,
  guides: null,
  qrTimer: null,
  logSince: 0,
  logOpen: false,
  verifying: false,
  netFilled: false,
  curRouter: 0,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

async function api(path, body) {
  const opt = { headers: {} };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(path, opt);
    return await r.json();
  } catch (e) {
    return { ok: false, msg: '请求失败：' + e };
  }
}

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

/* ── 步骤切换 ── */
function goStep(n, silent) {
  S.step = n;
  document.querySelectorAll('.stepc').forEach((el) => el.classList.remove('active'));
  const el = $('step-' + n);
  if (el) el.classList.add('active');
  document.querySelectorAll('#stepnav a').forEach((a) => {
    const s = Number(a.dataset.step);
    a.classList.toggle('active', s === n);
    a.classList.toggle('done', S.state && Number(S.state.step) > s);
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (!silent) api('/api/step', { step: n });

  if (n === 1) loadGuides();
  if (n === 3) renderNet();
  if (n === 4) renderLogin();
  if (n === 5) renderLive();
}

document.querySelectorAll('#stepnav a').forEach((a) => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    goStep(Number(a.dataset.step));
  });
});

function setLight(id, on, text, warn) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle('on', !!on);
  el.classList.toggle('warn', !!warn && !on);
  if (text) el.lastElementChild.textContent = text;
}

/* ── 轮询 ── */
async function tick() {
  const st = await api('/api/state');
  if (!st || st.error) return;
  S.state = st;

  const sv = st.services || {};
  const ps5 = (sv.nginx && sv.nginx.publisher) || {};
  const relay = sv.relay || {};
  const dm = sv.danmaku || {};
  const live = st.live || {};
  const hj = st.hijack || {};
  const dv = hj.divert || {};

  setLight('lt-hijack', hj.running, hj.running ? '直播桥已开' : '直播桥未开', true);
  setLight('lt-ps5', !!ps5.publishing, 'PS5 推流');
  setLight('lt-bili', live.state === 'live' || live.state === 'relaying',
    live.state === 'live' ? 'B 站直播中' : 'B 站直播');
  setLight('lt-dm', !!(sv.irc && sv.irc.ps5), '弹幕');

  if ($('ipchip')) $('ipchip').textContent = '本机 ' + (st.lan_ip || '未探测');
  if ($('rail-ip')) $('rail-ip').textContent = st.lan_ip || '—';
  if ($('rail-mode')) $('rail-mode').textContent = hj.running ? '已开启' : '未开启';
  if ($('s3-lanip')) $('s3-lanip').textContent = st.lan_ip || '未探测';

  // 第 1 步：直播桥按钮与状态
  if ($('s1-btn-bridge')) {
    const routerMode = !dv.running && !!st.progress_dns_ok;
    $('s1-btn-bridge').textContent = dv.running ? '重启直播桥' : '开启直播桥';
    $('s1-bridge-state').textContent = dv.running
      ? '已开启，接住 ' + (dv.total || 0) + ' 条连接'
      : (routerMode ? '未开启（路由器已生效，不用它）' : '未开启');
    $('s1-badge').textContent = dv.running ? '已用直播桥'
      : (routerMode ? '已用路由器' : '未配置');
    $('s1-badge').className = 'badge ' + ((dv.running || routerMode) ? 'ok' : '');
  }
  const err = hj.error || dv.error || '';
  if ($('s1-msg') && S.step === 1) {
    $('s1-msg').innerHTML = err
      ? `<div class="note err">直播桥开不了：${esc(err)}</div>`
      : '';
  }

  // 第 5 步面板
  const stateMap = {
    idle: '未开始', waiting: '等 PS5', starting: '正在开播',
    relaying: '转发中', live: '直播中', local: '本地取流', error: '出错',
  };
  if ($('m-ps5')) {
    $('m-ps5').textContent = ps5.publishing ? '已收到' : '等待';
    $('m-bili').textContent = live.state === 'live' ? '直播中'
      : (live.state === 'relaying' ? '已开播' : (live.state === 'error' ? '失败' : '—'));
    $('m-relay').textContent = relay.running ? '转发中' : '未转发';
    $('m-quality').innerHTML = ps5.publishing
      ? `${ps5.width || 0}×${ps5.height || 0}<br><small>${ps5.bw_in || 0} kbps</small>`
      : '—';
    $('m-uptime').textContent = fmtDur(live.uptime);
    $('m-dmcount').textContent = dm.count || 0;
    $('m-irc').textContent = (sv.irc && sv.irc.ps5) ? '已连' : '未连';
    $('m-room').textContent = (st.bili && st.bili.room_id) ? st.bili.room_id : '—';
    $('s6-badge').textContent = stateMap[live.state] || live.state;
    $('s6-badge').className = 'badge ' + (live.state === 'live' ? 'ok'
      : live.state === 'error' ? 'err' : '');
    $('s5-badge').textContent = stateMap[live.state] || '等 PS5';
    $('s5-badge').className = 'badge ' + (live.state === 'live' ? 'ok'
      : live.state === 'error' ? 'err' : 'warn');

    if (live.error) {
      $('s5-msg').innerHTML = `<div class="note err">出错：${esc(live.error)}</div>`;
    } else if (live.state === 'live') {
      $('s5-msg').innerHTML = `<div class="note ok">B 站直播间已在播 PS5 画面。手机发条弹幕，会显示在 PS5 画面上。</div>`;
    } else if (live.state === 'relaying') {
      $('s5-msg').innerHTML = `<div class="note">等 B 站确认开播，一般 3 到 10 秒。</div>`;
    } else if (live.state === 'local') {
      $('s5-msg').innerHTML = `<div class="note ok">画面已接住。复制下面的地址，
        在 OBS 里加一个「媒体源」拉取，再用 OBS 推流到 B 站。</div>`;
    } else if (st.bili && !st.bili.logged_in) {
      $('s5-msg').innerHTML = `<div class="note warn">还没登录 B 站，先去
        <a href="#" onclick="goStep(4);return false">第 4 步</a> 扫码。</div>`;
    } else if (!hj.running && !st.progress_dns_ok) {
      // 路由器方案成功时不需要直播桥，这种不算错
      $('s5-msg').innerHTML = `<div class="note err">直播桥没开，DNS 也没指到本机。回
        <a href="#" onclick="goStep(1);return false">第 1 步</a> 处理。</div>`;
    } else {
      $('s5-msg').innerHTML = `<div class="note">去 PS5 点「开始直播」，选 Twitch。</div>`;
    }
    if (st.bili && st.bili.room_id) {
      $('link-room').style.display = '';
      $('link-room').href = 'https://live.bilibili.com/' + st.bili.room_id;
    }
    renderOutputMode();
  }

  if (st.bili && st.bili.logged_in && $('s4-badge')) {
    $('s4-badge').textContent = st.bili.uname || '已登录';
    $('s4-badge').className = 'badge ok';
    $('s4-btn-logout').classList.remove('hidden');
  }

  renderDanmaku(st.danmaku_recent || []);
  pullLogs();
}

/* ── 第 1 步：路由器 + 直播桥 ── */
async function loadGuides() {
  if (!S.guides) {
    const g = await api('/api/guides');
    if (!g || g.error) return;
    S.guides = g;
    if ($('s1-ip')) $('s1-ip').textContent = g.ip || '—';
    renderGuides();
  }
}

function renderGuides() {
  const g = S.guides;
  if (!g || !$('router-tabs')) return;
  const tabs = $('router-tabs');
  tabs.innerHTML = '';
  g.guides.forEach((it, i) => {
    const b = document.createElement('button');
    b.className = 'tab' + (i === S.curRouter ? ' active' : '');
    b.textContent = it.title;
    b.onclick = () => { S.curRouter = i; renderGuides(); };
    tabs.appendChild(b);
  });
  const it = g.guides[S.curRouter];
  let html = '<ol class="steps">';
  it.steps.forEach((s) => { html += '<li>' + esc(s) + '</li>'; });
  if (it.format === 'table' && it.rows) {
    html += '</ol><table class="mini"><tr><th>域名</th><th>IP</th></tr>';
    it.rows.forEach((r) => {
      html += `<tr><td class="mono">${esc(r.domain)}</td><td class="mono">${esc(r.ip)}</td></tr>`;
    });
    html += '</table>';
  } else {
    html += '</ol><div class="codebox" data-code="' + esc(it.snippet) + '">'
      + '<button class="copy" onclick="copyCode(this)">复制</button>'
      + esc(it.snippet) + '</div>';
  }
  if (it.note) html += '<div class="note warn">' + esc(it.note) + '</div>';
  $('router-body').innerHTML = html;
}

function copyCode(btn) {
  const code = btn.parentElement.dataset.code;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 1400);
  });
}

async function exportRouterFiles() {
  const r = await api('/api/guides/export', {});
  alert(r.ok ? '已导出到程序目录的「路由器配置」文件夹：\n' + (r.files || []).join('\n')
             : '导出失败：' + (r.msg || ''));
}

async function toggleBridge() {
  const btn = $('s1-btn-bridge');
  const dv = ((S.state && S.state.hijack) || {}).divert || {};
  btn.disabled = true;
  btn.textContent = '处理中…';
  if (dv.running) await api('/api/divert/stop', {});
  const r = await api('/api/divert/start', {});
  btn.disabled = false;
  if (r && !r.ok && r.detail) {
    $('s1-msg').innerHTML = `<div class="note err">开启失败：${esc(r.detail)}</div>`;
  }
  await tick();
  renderGuides();
}

/* ── 第 2 步：检测 ── */
async function runVerify() {
  if (S.verifying) return;
  S.verifying = true;
  const btn = $('s2-btn');
  btn.disabled = true;
  btn.textContent = '正在检测…';
  $('s2-verdict').innerHTML = `<div class="note">大约 2 秒。</div>`;
  $('s2-results').innerHTML = '';

  const r = await api('/api/dns/verify', { mode: 'router' });
  S.verifying = false;
  btn.disabled = false;
  btn.textContent = '再检测一次';

  if (!r || r.error) {
    $('s2-verdict').innerHTML = `<div class="note err">检测失败：${esc(r && r.msg || '未知错误')}</div>`;
    return;
  }

  $('s2-badge').textContent = r.ok ? '通过' : '没通过';
  $('s2-badge').className = 'badge ' + (r.ok ? 'ok' : 'err');
  $('s2-verdict').innerHTML = r.ok
    ? `<div class="verdict ok"><div class="icon">✓</div><div>
         <h4>生效了</h4><p>${esc(r.hint)}</p></div></div>
       <h3>PS5 用哪种加速</h3>
       <p class="small dim" style="margin-top:-6px">
         看加速器装在哪。两种都能用，只有第二种要去 PS5 改设置。
       </p>
       <div class="flexline">
         <button class="btn primary" onclick="chooseRouterAccel(this)">
           装路由器上（PS5 不用改）
         </button>
         <button class="btn" onclick="choosePcAccel()">
           装电脑上（要填四个数）
         </button>
       </div>
       <p class="small dim" id="s2-pick-note"></p>`
    : `<div class="verdict bad"><div class="icon">✕</div><div>
         <h4>还没通</h4><p>${esc(r.hint)}</p></div></div>
       <div class="btn-row">
         <button class="btn" onclick="goStep(1)">回上一步改路由器</button>
         <button class="btn primary" onclick="useBridgeFromVerify(this)">改用直播桥</button>
       </div>`;

  const rows = (r.results || []).map((s) => {
    const pct = s.total ? Math.round(s.hits / s.total * 100) : 0;
    return `<div class="srcrow">
      <span class="name">${esc(s.label)}</span>
      <span class="tick ${s.ok ? 'y' : 'n'}">${s.ok ? '✓' : '✕'}</span>
      <span class="bar"><i class="${s.ok ? 'full' : ''}" style="width:${pct}%"></i></span>
      <span class="mono small dim">${s.hits}/${s.total}</span>
    </div>`;
  }).join('');
  $('s2-results').innerHTML = rows ? '<h3>各来源</h3><div class="checklist">' + rows + '</div>' : '';

  // 逐条明细 + 直接说哪儿不对
  const src = (r.results || []).find((s) => s.kind === 'router') || (r.results || [])[0];
  if (src && (src.rows || []).length) {
    const lan = (S.state && S.state.lan_ip) || '';
    const list = src.rows.map((row) => `
      <div class="checkrow">
        <span class="q">${esc(row.domain)}</span>
        <span class="a"><span class="tick ${row.ok ? 'y' : 'n'}">${row.ok ? '✓' : '✕'}</span>
        <span class="mono small">${esc((row.ips || []).join(', ') || row.err || '没答上来')}</span></span>
      </div>`).join('');
    $('s2-results').insertAdjacentHTML('beforeend',
      `<h3>${esc(src.label)} 逐条答复</h3><div class="checklist">${list}</div>`);

    const why = diagnose(src, lan);
    if (why) $('s2-results').insertAdjacentHTML('beforeend', why);
  }
}

/* 根据「路由器实际答了什么」直接给出改法，别让人自己猜 */
function diagnose(src, lan) {
  const priv = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/;
  const counts = {};
  (src.rows || []).forEach((row) => {
    (row.ips || []).forEach((ip) => {
      if (priv.test(ip)) counts[ip] = (counts[ip] || 0) + 1;
    });
  });
  let top = '', n = 0;
  Object.keys(counts).forEach((ip) => {
    if (counts[ip] > n) { n = counts[ip]; top = ip; }
  });

  if (top && top !== lan && n >= 2) {
    return `<div class="note err">路由器 hosts 里填的是 <b class="mono">${esc(top)}</b>，
      这台电脑是 <b class="mono">${esc(lan || '?')}</b>。把 ${esc(top)} 改成 ${esc(lan || '本机 IP')} 再测。</div>`;
  }
  const missed = (src.rows || []).filter((row) => !row.ok).map((row) => row.domain);
  if (missed.length) {
    return `<div class="note warn">这几个域名没生效：<b>${esc(missed.join('、'))}</b>。
      可能 hosts 漏了，或路由器没重启 DNS。</div>`;
  }
  return '';
}

async function useBridgeFromVerify(btn) {
  btn.disabled = true;
  btn.textContent = '正在开启…';
  const r = await api('/api/divert/start', {});
  btn.disabled = false;
  btn.textContent = '改用直播桥';
  if (r && r.ok) {
    $('s2-badge').textContent = '已用直播桥';
    $('s2-badge').className = 'badge ok';
    $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
      <h4>直播桥已开</h4><p>PS5 的推流直接接到本机，不用改路由器。</p>
      </div></div><div class="btn-row">
      <button class="btn primary" onclick="goStep(3)">下一步 →</button></div>`;
    tick();
  } else {
    $('s2-verdict').insertAdjacentHTML('beforeend',
      `<div class="note err">开启失败：${esc(r && r.detail || '未知原因')}</div>`);
  }
}

/* 路由器上装了加速器：PS5 什么都不用改。
   原理：hosts 已把推流域名指到本机，PS5 同网段直连，路由器加速器
   只加速去外网的流量，不碰内网地址。
   直播桥这时留着也不碍事：PS5 连的是本机地址，劫持只处理发往远端的连接。 */
async function chooseRouterAccel(btn) {
  btn.disabled = true;
  $('s2-pick-note').innerHTML = 'PS5 保持「自动获取」，什么都不用改。<br>'
    + '直播桥不用关，它不碰 PS5 直连本机的流量。<br>'
    + '去登录 B 站，然后直接在 PS5 上开播。';
  await tick();
  setTimeout(() => goStep(4), 900);
}

/* 电脑上装了加速器：必须有直播桥，PS5 要填四个数 */
async function choosePcAccel() {
  const dv = ((S.state && S.state.hijack) || {}).divert || {};
  if (!dv.running) {
    const r = await api('/api/divert/start', {});
    if (r && !r.ok) alert('直播桥没开起来：' + (r.detail || '未知原因'));
    await tick();
  }
  goStep(3);
}

/* ── 第 3 步：PS5 网络 ── */
async function renderNet() {
  if (!S.env) S.env = await api('/api/env');
  const env = S.env || {};
  const st = S.state || {};
  const net = (st.config && st.config.ps5_net) || {};

  if (!S.netFilled) {
    $('in-ip').value = net.ip || '';
    $('in-mask').value = net.mask || '';
    $('in-gw').value = net.gateway || '';
    $('in-dns').value = net.dns || '';
    S.netFilled = true;
  }
  if ($('s3-lanip')) $('s3-lanip').textContent = st.lan_ip || '未探测';

  const accel = env.accel_procs || [];
  $('s3-badge').textContent = accel.length ? '加速器在跑' : '没检测到加速器';
  $('s3-badge').className = 'badge ' + (accel.length ? 'ok' : 'warn');
  $('s3-accel').innerHTML = accel.length
    ? `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>加速器：${esc(accel.join('、'))}</h4>
        <p>确认它在给 PS5 加速。它给的四个数，下面照抄。</p>
        </div></div>`
    : `<div class="verdict bad"><div class="icon">!</div><div>
        <h4>没检测到加速器</h4>
        <p>先打开加速器并给 PS5 加速。没有它，PS5 连不上 Twitch。</p>
        </div></div>`;
}

async function autofillNet() {
  const r = await api('/api/ps5net/autofill');
  if (!r) return;
  if (r.gateway) $('in-gw').value = r.gateway;
  if (r.dns) $('in-dns').value = r.dns;
  $('s3-fillnote').textContent = '网关和 DNS 已填，IP 和掩码照加速器抄。';
}

async function saveNet() {
  const ip = $('in-ip').value.trim();
  const mask = $('in-mask').value.trim();
  const gw = $('in-gw').value.trim();
  const dns = $('in-dns').value.trim();
  if (!ip || !mask || !gw || !dns) {
    if (!confirm('四项没填全。不影响使用，只是查不到这次参数。还保存吗？')) return;
  }
  const r = await api('/api/ps5net', { ip, mask, gateway: gw, dns });
  if (r && r.ok) {
    $('s3-fillnote').textContent = '已保存。';
    tick();
  } else {
    alert('保存失败：' + (r && (r.msg || r.detail) || '未知错误'));
  }
}

/* ── 第 4 步：登录 ── */
async function renderLogin() {
  const st = S.state || {};
  const b = st.bili || {};
  if (b.logged_in) {
    $('s4-badge').textContent = b.uname || '已登录';
    $('s4-badge').className = 'badge ok';
    $('s4-btn-logout').classList.remove('hidden');
    $('s4-status').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
      <h4>${esc(b.uname || '已登录')}</h4>
      <p>直播间 <span class="mono">${esc(b.room_id || '—')}</span></p></div></div>`;
    $('qrbox').className = 'qrbox empty';
    $('qrbox').textContent = '已登录，不用扫码';
    loadAreas();
  }
}

async function newQr() {
  const btn = $('s4-btn-qr');
  btn.disabled = true;
  btn.textContent = '生成中…';
  const r = await api('/api/qr/new', {});
  btn.disabled = false;
  btn.textContent = '换个二维码';
  if (!r.ok) { alert('生成失败：' + (r.msg || '')); return; }

  $('qrbox').className = 'qrbox';
  $('qrbox').innerHTML = `<img src="${r.png}" alt="B站登录二维码">`;
  $('s4-status').innerHTML = `<p><b>手机 B 站 App 扫这个码</b></p>
    <p class="small dim">扫完在手机上点确认，3 分钟内有效。</p>`;

  clearInterval(S.qrTimer);
  S.qrTimer = setInterval(async () => {
    const p = await api('/api/qr/poll');
    if (p.status === 'scanned') {
      $('s4-status').innerHTML = `<p><b>已扫到，手机上点确认</b></p>`;
    } else if (p.status === 'success') {
      clearInterval(S.qrTimer);
      $('qrbox').className = 'qrbox empty';
      $('qrbox').textContent = '登录成功';
      $('s4-status').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>${esc(p.uname || '')}</h4>
        <p>直播间 <span class="mono">${esc(p.room_id || '—')}</span>。弹幕已连上。</p>
        </div></div>`;
      $('s4-badge').textContent = p.uname || '已登录';
      $('s4-badge').className = 'badge ok';
      $('s4-btn-logout').classList.remove('hidden');
      loadAreas();
      tick();
    } else if (p.status === 'expired') {
      clearInterval(S.qrTimer);
      $('s4-status').innerHTML = `<div class="note warn">二维码过期了，点「换个二维码」。</div>`;
    }
  }, 2000);
}

async function doLogout() {
  if (!confirm('退出登录要重新扫码，确定？')) return;
  await api('/api/logout', {});
  $('s4-badge').textContent = '未登录';
  $('s4-badge').className = 'badge';
  $('s4-btn-logout').classList.add('hidden');
  $('s4-status').innerHTML = `<p class="dim">已退出。</p>`;
  tick();
}

async function loadAreas() {
  const r = await api('/api/partitions');
  const sel = $('sel-area');
  if (!r || !r.partitions) return;
  sel.innerHTML = '';
  const cur = String(r.current || '236');
  Object.keys(r.partitions).forEach((parent) => {
    const og = document.createElement('optgroup');
    og.label = parent;
    Object.entries(r.partitions[parent]).forEach(([name, id]) => {
      const o = document.createElement('option');
      o.value = id;
      o.textContent = parent + ' / ' + name;
      if (String(id) === cur) o.selected = true;
      og.appendChild(o);
    });
    sel.appendChild(og);
  });
  const st = S.state || {};
  $('in-title').value = (st.config && st.config.bilibili_title) || '';
}

async function saveArea() {
  const r = await api('/api/area', { area_id: $('sel-area').value, title: $('in-title').value.trim() });
  if (r && r.ok) {
    const opt = $('sel-area').selectedOptions[0];
    alert('已保存：' + (opt ? opt.textContent : ''));
  } else alert('保存失败');
}

/* ── 第 5 步：开播 ── */
function renderLive() {
  const st = S.state || {};
  const b = st.bili || {};
  if (!b.logged_in && $('s5-msg')) {
    $('s5-msg').innerHTML = `<div class="note warn">还没登录 B 站，先去
      <a href="#" onclick="goStep(4);return false">第 4 步</a> 扫码。</div>`;
  }
}

function renderOutputMode() {
  const st = S.state || {};
  const live = st.live || {};
  const mode = live.output_mode || 'direct';
  if ($('pick-direct')) {
    $('pick-direct').classList.toggle('on', mode === 'direct');
    $('pick-local').classList.toggle('on', mode === 'local');
  }
  const box = $('s5-local');
  if (!box) return;
  if (mode !== 'local') { box.innerHTML = ''; return; }

  const url = live.local_url || '';
  const ready = live.state === 'local';
  box.innerHTML = `
    <div class="urlbox">
      <span class="mono" id="s5-url">${esc(url || '等 PS5 开始直播后出现地址')}</span>
      ${ready ? '<button class="btn sm" onclick="copyUrl()">复制</button>' : ''}
    </div>
    <p class="small dim">
      OBS 里加「媒体源」→ 取消勾选「本地文件」→ 把这个地址填进去。
      拉流后由 OBS 推流到 B 站。这一模式不会替你开播，也不需要登录 B 站。
    </p>`;
}

function copyUrl() {
  const el = $('s5-url');
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    el.textContent = '已复制';
    setTimeout(renderOutputMode, 1200);
  });
}

async function setOutputMode(mode) {
  const r = await api('/api/output/mode', { mode });
  if (r && !r.ok) { alert('切换失败：' + (r.msg || '未知原因')); return; }
  await tick();
  renderOutputMode();
}

async function manualStart() {
  const r = await api('/api/live/start', {});
  if (!r.ok) alert(r.msg || '开播失败');
  tick();
}

async function stopLive() {
  if (!confirm('停止直播？B 站会一起关播，PS5 那边请自己停。')) return;
  await api('/api/live/stop', { reason: '用户手动停止' });
  tick();
}

async function testInject() {
  const r = await api('/api/test/inject', { text: '' });
  alert(r.ok ? '发了，看 PS5 画面。' : (r.msg || '发送失败'));
}

/* ── 弹幕 / 日志 / 退出 ── */
function renderDanmaku(list) {
  const box = $('dmlist');
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<div class="dmrow"><span class="m dim">还没有</span></div>';
    return;
  }
  box.innerHTML = list.map((d) => `
    <div class="dmrow ${esc(d.kind)}">
      <span class="t">${esc(d.t || '')}</span>
      <span class="u">${esc(d.user || '')}</span>
      <span class="m">${esc(d.text || '')}${d.price ? ` <span class="dim">¥${(d.price / 1000).toFixed(1)}</span>` : ''}</span>
    </div>`).join('');
}

function toggleLog() {
  S.logOpen = !S.logOpen;
  $('logbar').classList.toggle('open', S.logOpen);
  $('loghint').textContent = S.logOpen ? '点击收起' : '点击展开';
}

async function pullLogs() {
  const r = await api('/api/logs?since=' + S.logSince);
  if (!r || !r.items || !r.items.length) return;
  S.logSince = r.last_ts || S.logSince;
  const body = $('logbody');
  r.items.forEach((it) => {
    const div = document.createElement('div');
    div.className = 'logline ' + it.level;
    div.innerHTML = `<span class="t">${esc(it.t)}</span><span class="lv">${esc(it.level)}</span><span class="m">${esc(it.msg)}</span>`;
    body.appendChild(div);
  });
  while (body.childNodes.length > 400) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
}

async function quitApp() {
  if (!confirm('退出会停掉收流和转发，并让 B 站关播。确定退出？')) return;
  await api('/api/shutdown', {});
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#8b93a7;'
    + 'font-family:system-ui">已退出，可以关掉这个窗口。</div>';
}

/* ── 启动 ── */
(async function boot() {
  S.env = await api('/api/env');
  const env = S.env || {};
  const st = await api('/api/state');
  S.state = st;

  // ?step=N 可以直接跳；否则按上次进度，第一次进来就是第 1 步
  const q = new URLSearchParams(location.search);
  const forced = Number(q.get('step') || 0);
  let step = forced || Number((st && st.step) || 1);
  if (step < 1 || step > 5) step = 1;
  goStep(step, true);

  tick();
  setInterval(tick, 1500);
})();
