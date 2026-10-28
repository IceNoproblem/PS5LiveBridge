

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

let CAPS = { platform: 'windows', divert: true, ps5_net_step: true,
             builtin_dns: true, gui: true, shutdown: true };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const isDocker = () => CAPS.platform === 'docker';

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

function setRail(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.classList.remove('ok', 'warn', 'err');
  if (cls) el.classList.add(cls);
}

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}


function applyCaps(st) {
  if (!st || !st.caps) return;
  CAPS = st.caps;
  if (!isDocker()) return;

  const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
  const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? '' : 'none'; };

  setHtml('s1-title', '第 1 步 · 把推流地址指到这台机器');
  setHtml('s1-sub', 'PS5 开播时把画面推给 Twitch 的服务器。让下面这几个域名答成这台机器'
    + '（<span class="mono" id="s1-ip">—</span>），画面就先进来，再转去 B 站。');
  if ($('s1-ip')) $('s1-ip').textContent = st.lan_ip || '—';
  if ($('s1-ip2')) $('s1-ip2').textContent = st.lan_ip || '—';
  if ($('s1-ip3')) $('s1-ip3').textContent = st.lan_ip || '—';

  show('s1-docker-note', true);

  show('s1-pick', false);
  show('mode-dns', false);
  show('mode-divert', false);
  show('mode-hosts', true);

  show('lt-hijack', false);
  show('rail-mode-row', false);
  show('s3-desktop', false);
  show('s3-docker', true);
  show('btn-quit', false);
  if ($('s3-docker-ip')) $('s3-docker-ip').textContent = st.lan_ip || '—';
  setHtml('nav-3-label', 'PS5<small>保持自动就行</small>');
}


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


  {
    const dnsOn = !!(hj.dns_running);
    const divertOn = !!dv.running;
    const lightOn = divertOn || dnsOn;
    const lightText = divertOn ? '流量接管已开'
      : (dnsOn ? '内置 DNS 已开（未走流量接管）' : '流量接管未开');
    setLight('lt-hijack', lightOn, lightText, true);
  }
  setLight('lt-ps5', !!ps5.publishing, 'PS5 推流');
  setLight('lt-bili', live.state === 'live' || live.state === 'relaying',
    live.state === 'live' ? 'B 站直播中' : 'B 站直播');
  setLight('lt-dm', !!(sv.irc && sv.irc.ps5), '弹幕');

  if ($('ipchip')) $('ipchip').textContent = '本机 ' + (st.lan_ip || '未探测');

  if ($('ver-chip') && st.version) $('ver-chip').textContent = 'v' + st.version;
  if ($('rail-ip')) $('rail-ip').textContent = st.lan_ip || '—';


  if ($('rail-ps5seen-row')) {


    const showPs5 = (S.modePick === 'dns')
      || (!S.modePick && !!hj.dns_running);
    $('rail-ps5seen-row').style.display = showPs5 ? '' : 'none';
    if (showPs5) {
      const seen = !!hj.ps5_seen;
      const el = $('rail-ps5seen');
      if (el) {
        el.textContent = seen ? '已接入' : '未接入';
        el.className = 'rail-v ' + (seen ? 'ok' : 'warn');


        const onAirNow = live.state === 'live' || live.state === 'relaying';
        el.title = seen
          ? 'PS5 正在使用这台电脑的 DNS'
          : (onAirNow
            ? 'PS5 没查这台电脑的 DNS，但推流是通的 —— 说明它在走路由器 hosts 那条路'
            : 'PS5 还没来查这台电脑的 DNS。检查 PS5 的网络设置里 DNS 是不是填了 ' + (st.lan_ip || '本机 IP'));
      }
    }
  }

  if ($('rail-mode')) {
    let modeText = '未开启';
    if (dv.running) {
      modeText = '流量接管';
    } else if (hj.dns_running) {
      modeText = '内置 DNS';
    }
    $('rail-mode').textContent = modeText;
  }
  if ($('s3-lanip')) $('s3-lanip').textContent = st.lan_ip || '未探测';


  if ($('in-proxy-1st') && st.config && st.config.twitch_proxy
      && !$('in-proxy-1st').value && document.activeElement !== $('in-proxy-1st')) {
    $('in-proxy-1st').value = st.config.twitch_proxy;
  }


  ['s1-ip', 's1-ip2', 's1-ip3'].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = st.lan_ip || '—';
  });


  if (!window._twitchFilled && $('in-twitch') && st.config) {
    $('in-twitch').value = st.config.twitch_channel || '';
    if ($('in-proxy')) $('in-proxy').value = st.config.twitch_proxy || '';
    window._twitchFilled = true;
  }


  const stageMap = {
    idle: ['空闲', ''],
    waiting: ['等 PS5 推流', 'warn'],
    starting: ['正在开播', 'warn'],
    face_verify: ['等人脸验证', 'warn'],
    relaying: ['转发中', 'ok'],
    live: ['直播中', 'ok'],
    local: ['本地取流', 'ok'],
    error: ['出错', 'err'],
  };
  const [stageText, stageCls] = stageMap[live.state] || [live.state || '—', ''];
  setRail('rail-state', stageText, stageCls);


  const ch = (live.bili && live.bili.channel) || '';
  setRail('rail-channel',
    ch === 'electron' ? '客户端模式' : (ch === 'livehime' ? '直播姬模式' : '—'), '');

  setRail('rail-relay', relay.running ? '转发中' : '停止', relay.running ? 'ok' : '');


  let sTxt = '—', sCls = '';
  if (live.state === 'live' || live.state === 'relaying' || live.state === 'local') {
    sTxt = '已确认'; sCls = 'ok';
  } else if (live.state === 'starting' || live.state === 'face_verify') {
    sTxt = '确认中'; sCls = 'warn';
  }
  setRail('rail-stream', sTxt, sCls);


  const dnsMode = st.dns_mode || 'router';
  let dnsText;
  if (dnsMode === 'builtin') {
    dnsText = '内置 DNS';
  } else if (isDocker()) {
    dnsText = '路由器';
  } else {
    dnsText = '端口劫持';
  }
  setRail('rail-dns', dnsText, 'ok');


  setRail('rail-dm', ((sv.danmaku && sv.danmaku.count) || 0) + ' 条', '');


  const onAir = live.state === 'local' || live.state === 'relaying' || live.state === 'live';
  setRail('rail-uptime', onAir ? fmtDur(live.uptime) : '—', '');


  const pub = (sv.nginx && sv.nginx.publisher) || {};
  if (pub.publishing && pub.width) {
    const res = pub.height >= 2160 ? '4K' : (pub.height >= 1440 ? '2K' : (pub.height >= 1080 ? '1080p' : (pub.height >= 720 ? '720p' : pub.height + 'p')));
    const mb = ((pub.bw_in || 0) / 1000).toFixed(1);
    setRail('rail-quality', res + (pub.fps ? pub.fps : '') + ' · ' + mb + 'M', 'ok');
  } else {
    setRail('rail-quality', '—', '');
  }


  if ($('s2-sub')) {
    const hj2 = (st.hijack) || {};
    if (dv.running) {
      $('s2-sub').innerHTML = '流量接管已经在拦推流连接了，<b>这一步其实不用检测</b>。'
        + '想确认就点一下，会直接告诉你已接管。';
    } else if (hj2.dns_running) {
      $('s2-sub').innerHTML = '内置 DNS 已经开着。下面检测的是<b>本机 DNS 能不能把推流域名答成本机 IP</b>'
        + '（PS5 那边用的就是它，所以只看这一项）。';
    } else {
      $('s2-sub').innerHTML = '拿上面那几个域名分别问路由器、本机、系统 DNS。'
        + '只要返回的 IP 是这台机器，就算通了。';
    }
  }


  const pick3 = S.modePick;
  if ($('s3-title')) {
    if (pick3 === 'hosts') {
      $('s3-title').textContent = '第 3 步 · PS5 什么都不用改';
    } else if (pick3 === 'dns') {
      $('s3-title').textContent = '第 3 步 · 改 PS5 的 DNS';
    } else if (pick3 === 'divert') {
      $('s3-title').textContent = '第 3 步 · 把加速器的四个数填进 PS5';
    } else {
      $('s3-title').textContent = (dv.running || hj.dns_running)
        ? '第 3 步 · 把加速器的四个数填进 PS5'
        : '第 3 步 · 确认 PS5 网络设置';
    }
  }


  const modeNote = $('s3-mode-note');
  const pk = S.modePick;
  if (modeNote) {
    if (pk === 'hosts') {
      modeNote.innerHTML = '<b>你走的是「改路由器 hosts」</b>：路由器已经把推流地址指过来了，'
        + '<b>PS5 保持「自动获取」就行，这一页一个数都不用填</b>。'
        + '下面这张表留着备查，填不填都不影响。';
    } else if (pk === 'dns') {
      const ip = (S.state && S.state.lan_ip) || '这台电脑的 IP';
      modeNote.innerHTML = '<b>你走的是「内置 DNS」</b>：'
        + '<b class="hl">DNS 那一栏填这台电脑的 IP（' + esc(ip) + '）</b>，'
        + 'IP / 掩码 / 网关填 PS5 <b>原来自动获取</b>的那组'
        + '（PS5 不允许 IP 自动、DNS 手动混着来）。';
    } else if (pk === 'divert') {
      modeNote.innerHTML = '<b>你走的是「流量接管」</b>：PS5 要连到这台电脑的加速器，'
        + '加速器给的四个数<b>照抄进 PS5</b>（下面这张表就是抄写位置）。';
    } else {

      if (dv.running) {
        modeNote.innerHTML = '<b>流量接管已开</b>：加速器给的四个数照抄进 PS5。';
      } else if (hj.dns_running) {
        const ip = (S.state && S.state.lan_ip) || '这台电脑的 IP';
        modeNote.innerHTML = '<b>内置 DNS 已开</b>：DNS 填这台电脑（' + esc(ip) + '），其余填原值。';
      } else {
        modeNote.innerHTML = '先在第一步选一种方案，这里会按你的情况给出填法。';
      }
    }


    if ($('s3-sub')) {
      $('s3-sub').textContent = (pk === 'hosts')
        ? '你走的是改路由器方案，PS5 这一页不用填，保持自动获取就行。'
        : (pk === 'dns'
          ? '只有 DNS 那一栏要改成本机 IP，其余三栏照 PS5 原来的值填。'
          : '加速器给四个数，四个都照抄进 PS5。');
    }
    if ($('s3-notfill')) {
      const lanip = (S.state && S.state.lan_ip) || '这台电脑的 IP';
      if (pk === 'hosts') {
        $('s3-notfill').innerHTML =
          'PS5 保持「自动获取」就行，一个数都不用填。';
      } else if (pk === 'dns') {
        $('s3-notfill').innerHTML =
          '这台电脑的 IP（<span class="mono">' + esc(lanip) + '</span>）'
          + '<b>只填在 DNS 那一栏</b>，IP / 掩码 / 网关 三栏填 PS5 原来的值。';
      } else {
        $('s3-notfill').innerHTML =
          '这台电脑的 IP（<span class="mono">' + esc(lanip) + '</span>）'
          + '不要填进 PS5，四个数照加速器给的抄。';
      }
    }


    if ($('nav-3-label')) {
      const label = (pk === 'hosts')
        ? 'PS5<small>什么都不用改</small>'
        : (pk === 'dns'
          ? 'PS5<small>只改 DNS</small>'
          : 'PS5<small>抄加速器的四个数</small>');
      $('nav-3-label').innerHTML = label;
    }


    const isHosts = pk === 'hosts';
    const isDns = pk === 'dns';
    if ($('th-accel')) {
      $('th-accel').textContent = isHosts ? '（不用填）' : (isDns ? '填在这里' : '加速器给的值');
    }
    const setFill = (id, normal, dnsText) => {
      const el = $(id);
      if (!el) return;
      el.textContent = isHosts ? 'PS5 保持自动' : (isDns ? dnsText : normal);
    };
    setFill('fill-ip', '加速器的「IP 地址」', 'PS5 原来的 IP');
    setFill('fill-mask', '加速器的「子网掩码」', 'PS5 原来的掩码');
    setFill('fill-gw', '加速器的「网关」', 'PS5 原来的网关');
    const dnsEl = $('fill-dns');
    if (dnsEl) {
      if (isHosts) {
        dnsEl.textContent = 'PS5 保持自动';
      } else if (isDns) {
        const ip = (S.state && S.state.lan_ip) || '本机 IP';
        dnsEl.innerHTML = '<b class="hl">这台电脑的 IP：' + esc(ip) + '</b>';
      } else {
        dnsEl.textContent = '加速器的「DNS」';
      }
    }
  }


  const bInfo = st.bili || {};
  setRail('rail-room', bInfo.room_id || '—', '');
  setRail('rail-title', bInfo.title || '—', '');


  ['s1-btn-dns-off', 's1-btn-dns-off2'].forEach((id) => {
    const b = $(id);
    if (b) b.style.display = hj.dns_running ? '' : 'none';
  });


  if ($('s1-btn-bridge')) {
    const routerMode = !dv.running && !hj.dns_running && !!st.progress_dns_ok;
    $('s1-btn-bridge').textContent = dv.running ? '重启接管' : '开启流量接管';
    const dnsOn1 = !!(hj.dns_running);


    const hostsOk = !!st.progress_dns_ok && !dnsOn1 && !dv.running;
    $('s1-bridge-state').textContent = dv.running
      ? '已开启，接住 ' + (dv.total || 0) + ' 条连接'
      : (dnsOn1 ? '未开启（当前走内置 DNS）'
        : (hostsOk ? '未开启（路由器 hosts 已验证通过）' : '未开启'));
    $('s1-badge').textContent = dv.running ? '已接管'
      : (dnsOn1 ? '已用内置 DNS' : (hostsOk ? '路由器 hosts 已生效' : '未配置'));
    $('s1-badge').className = 'badge ' + ((dv.running || routerMode) ? 'ok' : '');
  }


  const dvErr = String((dv && dv.error) || '').trim();
  const hjErr = String((hj && hj.error) || '').trim();
  if (S.step === 1) {
    const b2 = $('s1-msg2');
    if (b2) {
      b2.innerHTML = dvErr
        ? `<div class="note err">流量接管开不了：${esc(dvErr)}</div>` : '';
    }
    const b1 = $('s1-msg');
    if (b1) {

      b1.innerHTML = (hjErr && hjErr !== dvErr)
        ? `<div class="note err">内置 DNS 出问题：${esc(hjErr)}</div>` : '';
    }
  }


  const stateMap = {
    idle: '未开始', waiting: '等 PS5', starting: '正在开播',
    relaying: '转发中', live: '直播中', local: '本地取流', error: '出错',
    face_verify: '人脸验证',
  };
  if ($('m-ps5')) {
    $('m-ps5').textContent = ps5.publishing ? '已收到' : '等待';
    $('m-bili').textContent = live.state === 'live' ? '直播中'
      : (live.state === 'relaying' ? '已开播' : (live.state === 'error' ? '失败'
      : (live.state === 'face_verify' ? '待验证' : '—')));
    $('m-relay').textContent = relay.running ? '转发中' : '未转发';
    $('m-quality').innerHTML = ps5.publishing
      ? `${ps5.width || 0}×${ps5.height || 0}<br><small>${ps5.bw_in || 0} kbps</small>`
      : '—';

    const onAir = live.state === 'local' || live.state === 'relaying' || live.state === 'live';
    $('m-uptime').textContent = onAir ? fmtDur(live.uptime) : '—';
    $('m-dmcount').textContent = dm.count || 0;
    $('m-irc').textContent = (sv.irc && sv.irc.ps5) ? '已连' : '未连';
    $('m-room').textContent = (st.bili && st.bili.room_id) ? st.bili.room_id : '—';
    $('s6-badge').textContent = stateMap[live.state] || live.state;
    $('s6-badge').className = 'badge ' + (live.state === 'live' ? 'ok'
      : live.state === 'error' ? 'err' : (live.state === 'face_verify' ? 'warn' : ''));
    $('s5-badge').textContent = stateMap[live.state] || '等 PS5';
    $('s5-badge').className = 'badge ' + (live.state === 'live' ? 'ok'
      : live.state === 'error' ? 'err' : 'warn');

    if (live.error) {
      $('s5-msg').innerHTML = `<div class="note err">出错：${esc(live.error)}</div>`;
    } else if (live.state === 'face_verify') {
      const fu = live.face_url || '';
      $('s5-msg').innerHTML = `<div class="note warn">
        <b>B 站要求人脸验证。</b>请在手机 B 站 App 里完成认证，软件会每 30 秒自动重试。
        ${fu ? `<br>验证链接：<a href="${esc(fu)}" target="_blank">${esc(fu.slice(0, 50))}…</a>` : ''}
        <br>完成验证后等一会儿就能自动开播，也可以
        <a href="#" onclick="manualStart();return false">点这里手动重试</a>。</div>`;
    } else if (live.state === 'live') {
      $('s5-msg').innerHTML = `<div class="note ok">B 站直播间已在播 PS5 画面。手机发条弹幕，会显示在 PS5 画面上。</div>`;
    } else if (live.state === 'relaying') {
      $('s5-msg').innerHTML = `<div class="note">等 B 站确认开播，一般 3 到 10 秒。</div>`;
    } else if (live.state === 'local') {
      $('s5-msg').innerHTML = `<div class="note ok">画面已接住。复制下面的地址，
        在 OBS 里加一个「媒体源」拉取，再用 OBS 推流到 B 站。</div>`;
    } else if (st.bili && !st.bili.logged_in && live.output_mode !== 'local') {
      $('s5-msg').innerHTML = `<div class="note warn">还没登录 B 站，先去
        <a href="#" onclick="goStep(4);return false">第 4 步</a> 扫码。`
        + `用本地取流的话不需要登录，可以直接在 PS5 开播。</div>`;
    } else if (!hj.running && !st.progress_dns_ok) {

      const back = '回 <a href="#" onclick="goStep(1);return false">第 1 步</a> 处理。';
      $('s5-msg').innerHTML = '<div class="note err">'
        + (isDocker() ? '路由器还没把推流指过来。' + back
                      : '接管没开，DNS 也没指到本机。' + back)
        + '</div>';
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
  html += '</ol>';


  const copyText = it.format === 'table' && it.rows
    ? it.rows.map((r) => r.ip + ' ' + r.domain).join('\n')
    : (it.snippet || '');

  html += '<div class="confbox">';
  html += '<div class="confbox-h"><span>'
    + (it.format === 'table' && it.rows ? '要填的内容（域名 → IP）' : '要粘贴的内容')
    + '</span><span class="grow"></span>'
    + '<button class="copy" data-code="' + esc(copyText) + '" onclick="copyCode(this)">复制</button></div>';

  if (it.format === 'table' && it.rows) {
    html += '<table class="mini"><tr><th>域名</th><th>IP</th></tr>';
    it.rows.forEach((r) => {
      html += `<tr><td class="mono">${esc(r.domain)}</td><td class="mono">${esc(r.ip)}</td></tr>`;
    });
    html += '</table>';
  } else {
    html += '<pre class="codebox">' + esc(it.snippet) + '</pre>';
  }
  html += '</div>';

  if (it.note) html += '<div class="note warn">' + esc(it.note) + '</div>';
  $('router-body').innerHTML = html;
}

function copyCode(btn) {
  const code = btn.dataset.code || (btn.parentElement && btn.parentElement.dataset.code);
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
    const box = $('s1-msg2') || $('s1-msg');
    if (box) box.innerHTML = `<div class="note err">开启失败：${esc(r.detail)}</div>`;
  }
  await tick();
  renderGuides();
}


async function toggleBuiltinDns() {
  const btn = $('s1-btn-dns-off');
  const dns = (S.state && S.state.dns_mode) || 'router';
  if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
  try {
    if (dns === 'builtin') {
      await api('/api/builtin/stop', {});
      if ($('s1-msg')) {
        $('s1-msg').innerHTML = '<div class="note">内置 DNS 已关闭。</div>';
      }
    } else {
      const r = await api('/api/builtin/start', {});
      if (r && !r.ok && r.detail && $('s1-msg')) {
        $('s1-msg').innerHTML = `<div class="note err">开启失败：${esc(r.detail)}</div>`;
      }
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '关闭内置 DNS'; }
    await tick();
  }
}


async function runVerify() {
  if (S.verifying) return;
  S.verifying = true;


  const pick = S.modePick;
  const btn = $('s2-btn');
  btn.disabled = true;
  btn.textContent = '正在检测…';
  $('s2-verdict').innerHTML = `<div class="note">大约 2 秒。</div>`;
  $('s2-results').innerHTML = '';


  const stNow = S.state || {};
  const dvNow = (stNow.hijack && stNow.hijack.divert) || {};


  if (dvNow.running && (pick === 'divert' || !pick)) {
    S.verifying = false;
    btn.disabled = false;
    btn.textContent = '再检测一次';
    $('s2-badge').textContent = '已接管';
    $('s2-badge').className = 'badge ok';
    $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>流量接管已开启，这一步已经完成</h4>
        <p>流量接管是直接拦推流连接的，<b>不需要改任何域名</b>，所以不用看域名检测结果。</p>
      </div></div>
      <div class="note">已接住 ${dvNow.total || 0} 条连接。下一步去确认 PS5 那边的网络设置。</div>
      <div class="btn-row">
        <button class="btn primary" onclick="goStep(3)">下一步 →</button>
      </div>`;
    $('s2-results').innerHTML = '';
    return;
  }


  const hjNow = (stNow.hijack) || {};


  const verifyMode = (pick === 'dns' || (!pick && hjNow.dns_running))
    ? 'builtin' : 'router';
  const r = await api('/api/dns/verify', { mode: verifyMode });
  S.verifying = false;
  btn.disabled = false;
  btn.textContent = '再检测一次';

  if (!r || r.error) {
    $('s2-verdict').innerHTML = `<div class="note err">检测失败：${esc(r && r.msg || '未知错误')}</div>`;
    return;
  }

  $('s2-badge').textContent = r.ok ? '通过' : '没通过';
  $('s2-badge').className = 'badge ' + (r.ok ? 'ok' : 'err');

  if (r.ok && isDocker()) {

    $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
         <h4>生效了</h4><p>${esc(r.hint)}</p></div></div>
       <div class="note">加速器在路由器上，PS5 什么都不用改。下一步只是确认一下。</div>
       <div class="btn-row">
         <button class="btn primary" onclick="goStep(3)">下一步 →</button>
       </div>`;
  } else if (r.ok && verifyMode === 'builtin') {

    $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
         <h4>本机 DNS 工作正常</h4>
         <p>推流域名已经答成本机 IP（${esc((S.state && S.state.lan_ip) || '')}）。
         PS5 的 DNS 指向这台电脑，链路就通了。</p></div></div>
       <div class="note">下一步确认 PS5 那边的四个数怎么填，别照加速器填。</div>
       <div class="btn-row">
         <button class="btn primary" onclick="goStep(3)">下一步 →</button>
       </div>`;
  } else if (r.ok) {

    if (pick === 'hosts') {
      $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
           <h4>路由器 hosts 已生效</h4><p>${esc(r.hint)}</p></div></div>
         <div class="note">推流域名已经指向这台电脑，<b>PS5 什么都不用改</b>，保持自动获取即可。</div>
         <div class="btn-row">
           <button class="btn primary lg" onclick="goStep(3)">下一步 →</button>
         </div>`;
    } else if (pick === 'dns') {
      const ip = (S.state && S.state.lan_ip) || '这台电脑的 IP';
      $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
           <h4>本机 DNS 工作正常</h4><p>${esc(r.hint)}</p></div></div>
         <div class="note">接下来去 PS5 上把 <b>DNS 改成 ${esc(ip)}</b>，
         其余三栏保持原来的值（下一步会给具体填法）。</div>
         <div class="btn-row">
           <button class="btn primary lg" onclick="goStep(3)">下一步 →</button>
         </div>`;
    } else {

      $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
           <h4>生效了</h4><p>${esc(r.hint)}</p></div></div>
         <h3>你的加速器装在哪</h3>
         <p class="small dim" style="margin-top:-6px">
           这一步只是补全你的情况，选完会按对应方案给下一步。
         </p>
         <div class="flexline">
           <button class="btn primary" onclick="chooseRouterAccel(this)">
             装在路由器上
           </button>
           <button class="btn" onclick="choosePcAccel()">
             装在电脑上（要填四个数）
           </button>
         </div>
         <p class="small dim" id="s2-pick-note"></p>`;
    }
  } else if (isDocker()) {
    $('s2-verdict').innerHTML = `<div class="verdict bad"><div class="icon">✕</div><div>
         <h4>还没通</h4><p>${esc(r.hint)}</p></div></div>
       <div class="btn-row">
         <button class="btn primary" onclick="goStep(1)">回上一步改路由器</button>
       </div>
       <div class="note warn">
         路由器上要是开了加速器（UU、迅游的路由器插件之类），它可能把 PS5 的域名解析
         接到自己服务器上，hosts 就不生效了。这种路由器改用「端口转发」那条做法。
       </div>`;
  } else {
    $('s2-verdict').innerHTML = `<div class="verdict bad"><div class="icon">✕</div><div>
         <h4>还没通</h4><p>${esc(r.hint)}</p></div></div>
       <div class="btn-row">
         <button class="btn" onclick="goStep(1)">回上一步改路由器</button>
         <button class="btn primary" onclick="useBridgeFromVerify(this)">改用流量接管</button>
       </div>`;
  }

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
  btn.textContent = '改用流量接管';
  if (r && r.ok) {
    $('s2-badge').textContent = '已接管';
    $('s2-badge').className = 'badge ok';
    $('s2-verdict').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
      <h4>流量接管已开</h4><p>PS5 的推流直接接到本机，不用改路由器。</p>
      </div></div><div class="btn-row">
      <button class="btn primary" onclick="goStep(3)">下一步 →</button></div>`;
    tick();
  } else {
    $('s2-verdict').insertAdjacentHTML('beforeend',
      `<div class="note err">开启失败：${esc(r && r.detail || '未知原因')}</div>`);
  }
}


async function chooseRouterAccel(btn) {
  await pickMode('hosts', true);
  btn.disabled = true;
  $('s2-pick-note').innerHTML = 'PS5 保持「自动获取」，什么都不用改。<br>'
    + '接管不用关，它不碰 PS5 直连本机的流量。<br>'
    + '去登录 B 站，然后直接在 PS5 上开播。';
  await tick();
  setTimeout(() => goStep(4), 900);
}


async function choosePcAccel() {
  await pickMode('divert', true);
  const dv = ((S.state && S.state.hijack) || {}).divert || {};
  if (!dv.running) {
    const r = await api('/api/divert/start', {});
    if (r && !r.ok) alert('流量接管没开起来：' + (r.detail || '未知原因'));
    await tick();
  }
  goStep(3);
}


async function renderNet() {
  if (isDocker()) {

    if ($('s3-docker-ip')) $('s3-docker-ip').textContent = (S.state && S.state.lan_ip) || '—';
    return;
  }


  try {
    S.env = await api('/api/env');
  } catch (e) {
    if (!S.env) S.env = {};
  }
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


  const routerDone = (S.modePick === 'hosts')
    || (!S.modePick && !(dv.running) && !(S.state && S.state.hijack && S.state.hijack.dns_running)
        && !!(S.state && S.state.progress_dns_ok));
  if (accel.length) {
    $('s3-badge').textContent = '加速器在跑';
    $('s3-badge').className = 'badge ok';
    $('s3-accel').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>本机加速器：${esc(accel.join('、'))}</h4>
        <p>确认它在给 PS5 加速。它给的四个数，下面照抄。</p>
        </div></div>`;
  } else if (routerDone) {
    $('s3-badge').textContent = '加速器在路由器上';
    $('s3-badge').className = 'badge ok';
    $('s3-accel').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>本机没有加速器，这页不用填</h4>
        <p>你的加速器装在<b>路由器</b>上（推流域名已经指到这台电脑了），
        PS5 保持自动获取就行，下面这几个数一个都不用填。</p>
        </div></div>`;
  } else {
    $('s3-badge').textContent = '没找到加速器';
    $('s3-badge').className = 'badge warn';
    $('s3-accel').innerHTML = `<div class="verdict warn"><div class="icon">!</div><div>
        <h4>本机没找到加速器进程</h4>
        <p>两种可能：<br>
        · 加速器装在<b>路由器</b>上 → 正常，这页不用填，直接去第 4 步<br>
        · 加速器装在<b>这台电脑</b>上 → 请先打开它，再回来点「检测加速器」</p>
        </div></div>`;
  }
}

async function autofillNet() {
  const r = await api('/api/ps5net/autofill');
  if (!r) return;
  if (r.gateway) $('in-gw').value = r.gateway;
  if (r.dns) $('in-dns').value = r.dns;
  if (r.gateway || r.dns) {
    $('s3-fillnote').textContent = '网关和 DNS 已填，IP 和掩码照加速器抄。';
  } else {
    $('s3-fillnote').textContent = '本机没找到加速器，四个数请照加速器界面手动抄。';
  }


  await renderNet();
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
  const body = {
    area_id: $('sel-area').value,
    title: $('in-title').value.trim(),
  };
  if ($('in-twitch')) body.twitch_channel = $('in-twitch').value.trim();
  if ($('in-proxy')) body.twitch_proxy = $('in-proxy').value.trim();
  const r = await api('/api/area', body);
  if (r && r.ok) {


    if ($('in-proxy-1st') && $('in-proxy')) {
      $('in-proxy-1st').value = $('in-proxy').value;
    }
    const opt = $('sel-area').selectedOptions[0];
    alert('已保存：' + (opt ? opt.textContent : ''));
  } else alert('保存失败');
}


function renderLive() {
  const st = S.state || {};
  const b = st.bili || {};
  const local = (st.live && st.live.output_mode) === 'local';
  if (!b.logged_in && !local && $('s5-msg')) {
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


const LV_TEXT = {
  info: '信息', success: '成功', warn: '警告',
  error: '错误', step: '步骤', debug: '调试',
};

async function pullLogs() {
  const r = await api('/api/logs?since=' + S.logSince);
  if (!r || !r.items || !r.items.length) return;
  S.logSince = r.last_ts || S.logSince;
  const body = $('logbody');
  r.items.forEach((it) => {
    const div = document.createElement('div');
    div.className = 'logline ' + it.level;
    div.dataset.level = it.level || 'info';

    div.innerHTML =
      `<span class="t">${esc(it.t)}</span>`
      + `<span class="lv">${esc(LV_TEXT[it.level] || it.level)}</span>`
      + (it.tag ? `<span class="tg">${esc(it.tag)}</span>` : '')
      + `<span class="m">${esc(it.msg)}</span>`;
    body.appendChild(div);
  });
  while (body.childNodes.length > 600) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
  applyLogFilter();
}

function applyLogFilter() {
  const sel = $('log-filter');
  const mode = sel ? sel.value : '';
  document.querySelectorAll('#logbody .logline').forEach((el) => {
    const lv = el.dataset.level || 'info';
    let show = true;
    if (mode === 'warn') show = (lv === 'warn' || lv === 'error');
    else if (mode === 'step') show = (lv === 'step' || lv === 'error');
    el.style.display = show ? '' : 'none';
  });
}

function clearLogs() {
  const body = $('logbody');
  if (body) body.innerHTML = '';

  S.logSince = Date.now() / 1000;
}

async function copyLogs() {
  const lines = [];
  document.querySelectorAll('#logbody .logline').forEach((el) => {
    if (el.style.display === 'none') return;
    lines.push(el.innerText.replace(/\s+/g, ' ').trim());
  });
  const text = lines.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    if ($('loghint')) {
      const old = $('loghint').textContent;
      $('loghint').textContent = '已复制 ' + lines.length + ' 行';
      setTimeout(() => { $('loghint').textContent = old; }, 1500);
    }
  } catch (e) {
    alert('复制失败，请手动选中复制');
  }
}

async function quitApp() {
  if (isDocker()) {
    alert('容器里没有「退出」这个操作。\n要停服务：docker compose down\n只重启：docker compose restart');
    return;
  }
  if (!confirm('退出会停掉收流和转发，并让 B 站关播。确定退出？')) return;
  await api('/api/shutdown', {});
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#8b93a7;'
    + 'font-family:system-ui">已退出，可以关掉这个窗口。</div>';
}


function showUpdateBar(msg, btnLabel, btnFn) {
  $('update-msg').innerHTML = msg;
  const b = $('update-btn');
  b.textContent = btnLabel;
  b.onclick = btnFn;
  $('updatebar').style.display = 'flex';
}
function dismissUpdate() {
  $('updatebar').style.display = 'none';
}

async function checkUpdate(force) {
  try {
    const r = await api('/api/update/check' + (force ? '?force=1' : ''));
    if (!r || r.state !== 'available') {
      if (force) alert('当前已是最新版本 v' + (r && r.current || ''));
      return;
    }
    const ver = esc(r.latest);
    if (r.platform === 'docker') {
      showUpdateBar(`发现新版本 v${ver}（当前 v${esc(r.current)}）`,
        '复制更新命令', () => {
          const cmd = r.install_hint || 'docker compose pull && docker compose up -d';
          navigator.clipboard && navigator.clipboard.writeText(cmd);
          alert('命令已复制，在 NAS 终端里执行：\n\n' + cmd + '\n\n'
            + '想要全自动更新：compose 里启用 auto-update profile。');
        });
    } else {
      showUpdateBar(`发现新版本 v${ver}（当前 v${esc(r.current)}）`,
        '立即更新', doUpdate);
    }
  } catch (e) {  }
}

async function doUpdate() {
  const b = $('update-btn');
  b.disabled = true;
  b.textContent = '准备中…';
  const r = await api('/api/update/run', {});
  if (!r || !r.ok) {
    b.disabled = false;
    b.textContent = '立即更新';
    alert((r && r.detail) || '更新没能开始');
    return;
  }
  const timer = setInterval(async () => {
    const s = await api('/api/update/status');
    if (!s) return;
    if (s.state === 'downloading') {
      b.textContent = `下载中 ${s.percent || 0}%`;
    } else if (s.state === 'installing') {
      b.textContent = '正在重启…';
      $('update-msg').textContent = '正在应用更新，软件会自动重启';
    } else if (s.state === 'error') {
      clearInterval(timer);
      b.disabled = false;
      b.textContent = '重试';
      alert('更新失败：' + (s.error || '未知原因'));
    }
  }, 800);
}


(async function boot() {
  S.env = await api('/api/env');
  const env = S.env || {};
  const st = await api('/api/state');
  S.state = st;


  applyCaps(st);


  const q = new URLSearchParams(location.search);
  const forced = Number(q.get('step') || 0);
  let step = forced || Number((st && st.step) || 1);
  if (step < 1 || step > 5) step = 1;
  goStep(step, true);

  tick();
  setInterval(tick, 1500);


  setTimeout(() => checkUpdate(false), 30000);
  setInterval(() => checkUpdate(false), 3600 * 1000);
})();


function _rtBody() {
  return {
    ip: ($('rt-ip') || {}).value ? $('rt-ip').value.trim() : '',
    username: ($('rt-user') || {}).value ? $('rt-user').value.trim() : 'root',
    password: ($('rt-pass') || {}).value || '',
  };
}

async function routerDetect() {
  const msg = $('rt-msg'), badge = $('rt-badge');
  if (msg) msg.textContent = '探测中…';
  const r = await api('/api/router/detect', { ip: _rtBody().ip });
  if (msg) msg.textContent = r.msg || '';
  if (badge) {
    badge.textContent = r.ok ? 'OpenWrt' : '未知';
    badge.className = 'badge ' + (r.ok ? 'ok' : '');
  }
  if (r.host && $('rt-ip') && !$('rt-ip').value) $('rt-ip').value = r.host;
}

async function routerApply() {
  const b = _rtBody();
  if (!b.password) {
    if ($('rt-msg')) $('rt-msg').textContent = '请先填路由器密码';
    return;
  }
  const msg = $('rt-msg');
  if (msg) msg.textContent = '正在写入路由器…';
  const r = await api('/api/router/apply', b);
  if (msg) msg.textContent = r.msg || (r.ok ? '已写入' : '失败');
  if (r.ok) {
    if ($('rt-badge')) {
      $('rt-badge').textContent = '已配置';
      $('rt-badge').className = 'badge ok';
    }
    if ($('s1-badge')) {
      $('s1-badge').textContent = '已用路由器';
      $('s1-badge').className = 'badge ok';
    }
  }
}

async function routerRestore() {
  const b = _rtBody();
  if (!b.password) {
    if ($('rt-msg')) $('rt-msg').textContent = '请先填路由器密码';
    return;
  }
  const msg = $('rt-msg');
  if (msg) msg.textContent = '正在还原…';
  const r = await api('/api/router/restore', b);
  if (msg) msg.textContent = r.msg || (r.ok ? '已还原' : '失败');
  if (r.ok && $('rt-badge')) {
    $('rt-badge').textContent = '未配置';
    $('rt-badge').className = 'badge';
  }
}


function toggleLogPanel() {
  const shell = document.querySelector('.shell');
  if (!shell) return;
  shell.dataset.logTouched = '1';
  const hidden = shell.classList.toggle('log-hidden');
  const btn = $('logpanel-toggle');
  if (btn) btn.textContent = hidden ? '⟨' : '⟩';
  if (!hidden) {

    const body = $('logbody');
    if (body) body.scrollTop = body.scrollHeight;
  }
}


function toggleLog() { toggleLogPanel(); }


function syncTopbarHeight() {
  const tb = document.querySelector('.topbar');
  if (!tb) return;
  const h = Math.round(tb.getBoundingClientRect().height);
  if (h > 0) document.documentElement.style.setProperty('--topbar-h', h + 'px');
}
window.addEventListener('resize', syncTopbarHeight);
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', syncTopbarHeight);
} else {
  syncTopbarHeight();
}
setTimeout(syncTopbarHeight, 300);


function autoCollapseLog() {
  const shell = document.querySelector('.shell');
  if (!shell) return;
  if (shell.dataset.logTouched) return;
  if (window.innerWidth < 1400) {
    shell.classList.add('log-hidden');
    const btn = $('logpanel-toggle');
    if (btn) btn.textContent = '\u27e8';
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', autoCollapseLog);
} else {
  autoCollapseLog();
}


async function pickMode(which, silent) {


  if (isDocker() && !silent) return;

  const modes = ['hosts', 'dns', 'divert'];
  S.modePick = which;
  try { localStorage.setItem('ps5lb_mode', which); } catch (e) {}


  if (which !== 'dns' && !silent) {
    const dnsOn = !!(S.state && S.state.hijack && S.state.hijack.dns_running);
    if (dnsOn) {
      try {
        await api('/api/builtin/stop', {});
        await tick();
      } catch (e) {}
    }
  }

  modes.forEach((m) => {
    const box = $('mode-' + m);
    if (box) box.style.display = (m === which) ? '' : 'none';
    const card = $('pick-' + m);
    if (card) card.classList.toggle('active', m === which);
  });


  if (silent) return;
  const target = $('mode-' + which);
  if (target && target.scrollIntoView) {
    target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}


async function saveProxyAndStartDns() {
  const el = $('in-proxy-1st');
  const proxy = (el ? el.value : '').trim();
  if (!proxy) {
    alert('请先填代理服务器。\n\n这条路必须有一个能上外网的代理，'
      + '否则 Twitch 应用会打不开，也就点不到「开始直播」。');
    if (el) el.focus();
    return;
  }
  if (!/^(https?:\/\/)?[\w.-]+:\d+/.test(proxy)) {
    alert('代理格式看起来不对。\n正确写法类似：http://192.168.110.99:7890');
    return;
  }
  const st = S.state || {};


  const r = await api('/api/area', {
    area_id: (st.bili && st.bili.area_id) || '236',
    twitch_proxy: proxy,
  });
  if (!r || !r.ok) {
    alert('代理保存失败：' + ((r && r.msg) || '未知错误'));
    return;
  }
  if ($('in-proxy')) $('in-proxy').value = proxy;
  await useBuiltinDns();
}


(function restoreModePick() {
  try {
    let saved = localStorage.getItem('ps5lb_mode');
    if (saved === 'pc') saved = 'divert';
    if (saved === 'router') saved = 'hosts';
    if (saved === 'hosts' || saved === 'dns' || saved === 'divert') {
      S.modePick = saved;
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => pickMode(saved, true));
      } else {
        pickMode(saved, true);
      }
    }
  } catch (e) {}
})();


async function useBuiltinDns() {
  const box = $('router-nohosts');

  if (box) {
    box.style.display = '';
    box.innerHTML = '<div class="note">正在启动内置 DNS…（要管理员权限）</div>';
  }
  const r = await api('/api/builtin/start', {});
  const ip = (S.state && S.state.lan_ip) || '这台电脑的 IP';
  if (r && r.ok) {
    if (box) {
      box.innerHTML = '<div class="verdict ok"><div class="icon">✓</div><div>'
        + '<h4>内置 DNS 已开启</h4>'
        + '<p>去 PS5 上这样设（顺序别乱）：</p>'
        + '<ol style="margin:8px 0 0 18px;line-height:1.9">'
        + '<li>先看一眼<b>当前自动获取到的 IP / 掩码 / 网关</b>，记下来（拍照最快）</li>'
        + '<li>把 IP 地址设置改成「手动」，填<b>刚才记下的那三个值</b></li>'
        + '<li>DNS 也改成「手动」，填 <b class="hl">' + esc(ip) + '</b></li>'
        + '</ol>'
        + '</div></div>'
        + '<p class="small dim" style="margin-top:10px">'
        + '为什么三个值也要手填：PS5 不允许「IP 自动 + DNS 手动」混着来，'
        + '一改 DNS 就会强制把 IP 也切手动。填的都是它本来就在用的值，'
        + '不影响上网，只是把 DNS 换成这台电脑。</p>'
        + '<p class="small dim">加速器装在路由器上时不提供 IP / 掩码 / 网关，'
        + '那三个值来自路由器的 DHCP，照 PS5 现在显示的值抄就行。</p>'
        + '<div class="btn-row"><button class="btn sm ghost" onclick="stopBuiltinDns()">'
        + '关闭内置 DNS</button></div>';
    }
    await tick();
  } else {
    const why = (r && (r.detail || r.msg)) || '未知原因';
    if (box) {
      box.innerHTML = '<div class="note err">内置 DNS 启动失败：' + esc(why) + '<br>'
        + '常见原因：53 端口被占用（加速器或系统的 DNS 服务）、没有管理员权限。</div>';
    }
  }
}

async function stopBuiltinDns() {
  await api('/api/builtin/stop', {});
  location.reload();
}
