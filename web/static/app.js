

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
  if (!isDocker()) {
    // macOS 专属调整：只在 macos 生效，Windows 走原有路径完全不动。
    // 包一层 try：界面适配出问题不能连累 boot() 后面的 goStep，否则整个页面空白。
    if (CAPS.platform === 'macos') {
      try { applyMacCaps(st); } catch (e) { console.warn('macOS 界面适配失败', e); }
    }
    return;
  }

  const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
  const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? '' : 'none'; };

  setHtml('s1-title', '方案选择');
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
  show('sniff-block', false);

  show('lt-hijack', false);
  show('rail-mode-row', false);
  show('s3-desktop', false);
  show('s3-docker', true);
  // 容器里只有 nginx，没有 rtmf.exe。这个下拉选了 rtmf 收流就起不来，干脆不给选。
  show('impl-block', false);
  show('btn-quit', false);
  if ($('s3-docker-ip')) $('s3-docker-ip').textContent = st.lan_ip || '—';
  setHtml('nav-3-label', '网络配置<small>PS5 保持自动</small>');
}


// macOS 专属界面调整。只在 CAPS.platform === 'macos' 时调用，
// Windows 和容器走的还是原来那套，一个字都不改。
function applyMacCaps(st) {
  const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
  const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? '' : 'none'; };

  // 方案三的说明文字由模板按 platform 渲染（见 wizard.html 的 s1-divert-desc），
  // 这里只处理前端才需要动的东西

  // macOS 的收流端固定是 MediaMTX，没有 nginx / rtmf 可选
  show('impl-block', false);

  // 嗅探靠 pydivert（WinDivert 的 Python 绑定），只有 Windows 有。
  // macOS 上这个按钮一点就报「缺少 pydivert 组件」，而且也没必要：
  // MediaMTX 的 /v3/paths/list 直接就能看出有没有流、是谁在推。
  show('sniff-block', false);

  // 方案一的提示：macOS 版没有远程改路由器的账号密码框时也能手动改
  const btn = $('s1-btn-bridge');
  if (btn && CAPS.divert) btn.textContent = '开启流量接管（会请求一次密码）';
}


function goStep(n, silent) {
  S.step = n;
  document.querySelectorAll('.stepc').forEach((el) => el.classList.remove('active'));
  const el = $('step-' + n);
  if (el) el.classList.add('active');
  const key = String(n);
  document.querySelectorAll('#stepnav a').forEach((a) => {
    const s = String(a.dataset.step);
    a.classList.toggle('active', s === key);
    const sn = Number(s);
    a.classList.toggle('done', !isNaN(sn) && S.state && Number(S.state.step) > sn);
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (!silent && !isNaN(Number(n))) api('/api/step', { step: Number(n) });

  if (n === 1) loadGuides();
  if (n === 3) renderNet();
  if (n === 4) renderLogin();
  if (n === 5) { renderLive(); }
  if (key === 'info') loadDiag(1);
}

document.querySelectorAll('#stepnav a').forEach((a) => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    const ds = a.dataset.step;
    goStep(isNaN(Number(ds)) ? ds : Number(ds));
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
  applyState(st);
}

// SSE 推过来的状态直接进这里。以前每 1.5 秒新建一条 TCP，
// 一台机器能攒几百条 TIME_WAIT，还会把 netstat 输出撑到几百行干扰排查。
let _applyN = 0;
function applyState(st) {
  if (!st || st.error) return;
  S.state = st;
  _applyN += 1;
  if (_applyN % 8 === 1) {
    // 调试开关不用每次都问，隔几次查一遍就够
    api('/api/logs/debug').then((dbg) => applyDebugBtn(dbg && dbg.on)).catch(() => {});
  }
  loadRtmpImpl();
  loadNetcheck();

  const sv = st.services || {};
  const ps5 = (sv.nginx && sv.nginx.publisher) || {};
  const relay = sv.relay || {};
  const dm = sv.danmaku || {};
  const live = st.live || {};
  const hj = st.hijack || {};
  const dv = hj.divert || {};
  const outs = st.outputs || {};
  const channel = (live.bili && live.bili.channel) || '';


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

  if ($('rail-ver') && st.version) $('rail-ver').textContent = 'v' + st.version;
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


  // 两段链路分开：PS5→本机 / 本机→B站
  const _pub = (sv.nginx && sv.nginx.publisher) || {};
  const _ps5On = !!_pub.publishing;
  setRail('rail-ps5-link', _ps5On ? '已连' : '未连', _ps5On ? 'ok' : '');

  const _pushing = live.state === 'live' || live.state === 'relaying';
  const _pushOff = st.outputs && st.outputs.push_bili === false;
  setRail('rail-bili-link',
    _pushing ? '推流中' : (_pushOff ? '已关闭' : (_ps5On ? '开播中' : '未推')),
    _pushing ? 'ok' : (_pushOff ? '' : ''));

  setRail('rail-relay',
    _pushing ? (st.rtmp_impl === 'rtmpf' ? 'rtmp-forwarder' : 'ffmpeg') : '—',
    _pushing ? 'ok' : '');

  // 弹幕两段链路（第 5 步卡片）
  const _dmOn = !!(sv.danmaku && sv.danmaku.connected);
  const _ircOn = !!(sv.irc && sv.irc.ps5);
  if ($('m-dm-link')) {
    $('m-dm-link').className = 'v ' + (_dmOn ? 'ok' : 'err');
    $('m-dm-link').innerHTML = _dmOn
      ? '已连<br><small>B 站弹幕</small>' : '未连';
  }
  if ($('m-irc-link')) {
    $('m-irc-link').className = 'v ' + (_ircOn ? 'ok' : 'warn');
    $('m-irc-link').innerHTML = _ircOn
      ? '已连<br><small>PS5 进了直播间</small>'
      : (_ircOn === false && sv.irc && sv.irc.running
        ? '等待<br><small>PS5 还没进直播间</small>' : '未连');
  }



  const dnsMode = st.dns_mode || 'router';
  {
    const w = $('accel-warn');
    // 这段只在方案二（内置 DNS）显示。
    // 方案一改的是路由器 hosts，方案三是端口接管，两者都不碰 DNS，
    // 加速器开不开跟它们没关系，摆出来只会误导。
    // 容器版同样不显示：第 3 步已经写明 PS5 保持自动、加速器照常加速。
    //
    // 2026-11-04 issue #4：原来文案写的是「先关加速器、开播成功后再开」，
    // 但关掉加速器后 PS5 连 contribute.live-video.net 都解析不出来，压根开不了播。
    // 用户的实测是开着 UU 走方案一完全正常。文案已改，显示范围也一并收窄。
    const _showAccelTip = !isDocker() && dnsMode === 'builtin';
    if (w) w.style.display = _showAccelTip ? '' : 'none';
  }
  // 加速方案：三种走法的真实状态
  //
  // DNS_MODE 只有 builtin / router 两个值：
  //   方案一（路由器 hosts）→ router，端口接管没跑
  //   方案二（内置 DNS）    → builtin
  //   方案三（端口接管）    → router + divert 在跑
  // 所以 router 下要靠 divert 是否运行来区分一和三，
  // 不能像以前那样用 isDocker() 判断（Windows 上方案一会显示错）。
  const divertRunning = !!(st.hijack && st.hijack.running);
  let dnsText, dnsCls = '';
  if (dnsMode === 'builtin') {
    dnsText = '方案二·内置DNS';
    dnsCls = (st.hijack && st.hijack.dns_running) ? 'ok' : 'err';
  } else if (divertRunning) {
    dnsText = '方案三·端口接管';
    dnsCls = 'ok';
  } else {
    dnsText = '方案一·路由器hosts';
    dnsCls = 'ok';
  }
  setRail('rail-dns', dnsText, dnsCls);


  setRail('rail-dm', ((sv.danmaku && sv.danmaku.count) || 0) + ' 条', '');


  const onAir = live.state === 'local' || live.state === 'relaying' || live.state === 'live';
  setRail('rail-uptime', onAir ? fmtDur(live.uptime) : '—', '');


  const pub = (sv.nginx && sv.nginx.publisher) || {};


  if (S.step === 2 && Date.now() - (S.diagAt || 0) > 60000) {
    loadDiag();
  }


  {
    const ngOn = !!(sv.nginx && sv.nginx.running);
    if (ngOn) {
      setRail('rail-nginx', '运行中', 'ok');
    } else if (onAir) {
      setRail('rail-nginx', '异常（已停）', 'err');
      if ($('rail-nginx')) $('rail-nginx').title = '直播中出现这个说明收流服务挂了，推流会立刻中断';
    } else {
      setRail('rail-nginx', '未启动', 'warn');
    }
  }
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


  // 没在第 1 步选过方案时，用 dns_mode 反推，否则标题会显示成方案三的文案
  const pick3 = S.modePick
    || (((S.state && S.state.dns_mode) === 'builtin') ? 'dns'
        : ((dv.running) ? 'divert' : 'hosts'));
  if ($('s3-title')) {
    if (pick3 === 'hosts') {
      $('s3-title').textContent = '网络配置';
    } else if (pick3 === 'dns') {
      $('s3-title').textContent = '网络配置';
    } else if (pick3 === 'divert') {
      $('s3-title').textContent = '网络配置';
    } else {
      $('s3-title').textContent = (dv.running || hj.dns_running)
        ? '网络配置'
        : '网络配置';
    }
  }


  const modeNote = $('s3-mode-note');
  // modePick 是第 1 步选的方案。如果没选过（老配置 / 直接跑到第 3 步），
  // 就从 dns_mode 反推：builtin 说明在用方案二。
  const pk = S.modePick
    || (((S.state && S.state.dns_mode) === 'builtin') ? 'dns' : '');
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


    // 加速器建议：只有方案三依赖加速器，方案一/二不需要
    if ($('s3-accel-tip')) {
      $('s3-accel-tip').style.display = (pk === 'divert') ? '' : 'none';
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
          + '<b>一栏都不要填进 PS5</b>，四个数全部照加速器给的抄。';
      }
    }


    if ($('nav-3-label')) {
      const sub = (pk === 'hosts')
        ? 'PS5 什么都不用改'
        : (pk === 'dns' ? 'PS5 只改 DNS' : 'PS5 抄加速器的四个数');
      $('nav-3-label').innerHTML = '网络配置<small>' + sub + '</small>';
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
    // 「填什么」列只写来源，不能重复左边的项名
    setFill('fill-ip', '加速器里的 IP 地址', 'PS5 原来的 IP');
    setFill('fill-mask', '加速器里的子网掩码', 'PS5 原来的掩码');
    setFill('fill-gw', '加速器里的默认网关', 'PS5 原来的网关');
    const dnsEl = $('fill-dns');
    if (dnsEl) {
      if (isHosts) {
        dnsEl.textContent = 'PS5 保持自动';
      } else if (isDns) {
        const ip = (S.state && S.state.lan_ip) || '本机 IP';
        dnsEl.innerHTML = '<b class="hl">这台电脑的 IP：' + esc(ip) + '</b>';
      } else {
        dnsEl.textContent = '加速器里的 DNS';
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
    // 两段链路分开显示：PS5→本机、本机→B站
    const ps5On = !!ps5.publishing;
    $('m-ps5').className = 'v ' + (ps5On ? 'ok' : 'warn');
    $('m-ps5').innerHTML = ps5On
      ? `已连<br><small>${ps5.width || 0}×${ps5.height || 0}${ps5.fps ? ' ' + ps5.fps + 'fps' : ''} ${ps5.bw_in || 0}kbps</small>`
      : '未连';

    const pushing = live.state === 'live' || live.state === 'relaying';
    $('m-bili').className = 'v ' + (pushing ? 'ok'
      : (live.state === 'error' ? 'err' : (outs.push_bili ? 'warn' : '')));
    $('m-bili').innerHTML = pushing
      ? `推流中<br><small>${esc(channel || '已开播')}</small>`
      : (live.state === 'error' ? '失败'
        : (live.state === 'face_verify' ? '待验证'
        : (outs.push_bili ? '等待 PS5' : '已关闭')));

    // macOS 固定是 MediaMTX，不能按 rtmp_impl 推，那个字段只在 Windows/容器有意义
    const engine = CAPS.platform === 'macos'
      ? 'MediaMTX + ffmpeg'
      : (st.rtmp_impl === 'rtmpf' ? 'rtmp-forwarder' : 'nginx + ffmpeg');
    $('m-relay').className = 'v ' + (pushing ? 'ok' : '');
    $('m-relay').innerHTML = pushing
      ? `${engine}<br><small>原样转发</small>`
      : '空闲';
    $('m-quality').innerHTML = ps5.publishing
      ? `${ps5.width || 0}×${ps5.height || 0}<br><small>${ps5.bw_in || 0} kbps</small>`
      : '—';

    const onAir = live.state === 'local' || live.state === 'relaying' || live.state === 'live';
    $('m-uptime').textContent = onAir ? fmtDur(live.uptime) : '—';
    $('m-dmcount').textContent = dm.count || 0;
    $('m-irc').textContent = (sv.irc && sv.irc.ps5) ? '已连' : '未连';
    $('m-room').textContent = (st.bili && st.bili.room_id) ? st.bili.room_id : '—';
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

function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function kvRows(rows) {
  return rows.map((r) => {
    if (r[0] === '__group__') {
      return '<div class="kvgroup">' + esc(r[1]) + '</div>';
    }
    return '<div class="kvrow"><span>' + esc(r[0]) + '</span><b class="' + (r[2] || '') + '">'
      + esc(r[1]) + '</b></div>';
  }).join('');
}

async function loadDiag(force) {
  const bar = $('diag-verdict');
  if (bar) { bar.className = 'diagbar'; bar.textContent = '正在采集本机状态…'; }
  S.diagAt = Date.now();

  let d = null;
  const _plan = S.modePick || (() => {
    // 兜底：S.modePick 还没恢复时直接读本地存储，别让结论退回「按运行状态猜」
    try {
      let v = localStorage.getItem('ps5lb_mode') || '';
      if (v === 'pc') v = 'divert';
      if (v === 'router') v = 'hosts';
      return v;
    } catch (e) { return ''; }
  })();
  try { d = await api('/api/diag' + (_plan ? ('?plan=' + encodeURIComponent(_plan)) : '')); } catch (e) {}
  if (!d || d.error) {
    if (bar) {
      bar.className = 'diagbar err';
      bar.textContent = '诊断采集失败：' + ((d && d.error) || '接口没响应');
    }
    return;
  }

  const L = d.local || {};
  const rows = [];

  const rtmp = L.rtmp || {};
  const nginxOn = !!((L.nginx || {}).running);
  if (rtmp.listening) {


    const proc = String(rtmp.process || '').toLowerCase();
    const named = proc !== '';
    // 收流端可能是 nginx、rtmp-forwarder（进程名 rtmf.exe），或者 macOS 上的 mediamtx。
    // 少写一个就会把「自己的服务在跑」误报成「端口被别人占了」。
    const isOwn = /nginx|rtmf|rtmpf|rtmp-forwarder|mediamtx/.test(proc);
    const ok = nginxOn && (!named || isOwn);
    const label = ok
      ? '监听中'
      : ('被 ' + (rtmp.process || '别的进程') + ' 占用');
    rows.push(['收流 ' + rtmp.port, label, ok ? 'ok' : 'err']);
  } else {
    rows.push(['收流 ' + rtmp.port, '未监听', 'err']);
  }

  const irc = L.irc || {};
  rows.push(['弹幕 ' + irc.port, irc.listening ? '监听中' : '未监听', irc.listening ? 'ok' : 'warn']);

  const dns = L.dns || {};
  if (dns.mode === 'dns') {
    rows.push(['内置 DNS', dns.running ? '运行中' : '应开未开', dns.running ? 'ok' : 'err']);
  } else if (dns.running) {
    rows.push(['内置 DNS', '运行中', 'ok']);
  } else {
    rows.push(['内置 DNS', '没用到', '']);
  }

  const rl = L.relay || {};
  rows.push(['443 中继', rl.running ? '运行中' : '未启动', rl.running ? 'ok' : (dns.mode === 'dns' ? 'warn' : '')]);

  const fw = L.firewall || {};
  const fwAll = Object.keys(fw);
  const fwOk = fwAll.filter((k) => fw[k] === true).length;
  rows.push(['防火墙', fwOk + '/' + fwAll.length + ' 条已放行',
    fwOk === fwAll.length ? 'ok' : 'warn']);


  // 转发与推流
  const rly = L.relay_info || {};
  const rbox = $('diag-relay');
  if (rbox) {
    const rows3 = [];
    rows3.push(['转发进程', rly.running ? '运行中' : '未运行',
      rly.running ? 'ok' : '']);
    rows3.push(['推流方式', CAPS.platform === 'macos'
      ? 'MediaMTX + ffmpeg'
      : (rly.impl === 'rtmpf' ? 'rtmp-forwarder' : 'nginx+ffmpeg'), '']);
    if (rly.bitrate) rows3.push(['目标码率', rly.bitrate, '']);
    if (rly.restarts) rows3.push(['重启次数', rly.restarts + ' 次',
      rly.restarts > 3 ? 'warn' : '']);
    if (rly.error) rows3.push(['最近错误', rly.error.slice(0, 80), 'err']);
    rbox.innerHTML = kvRows(rows3);
  }

  // 端口全景
  const po = (L.ports && L.ports.items) || [];
  const pbox = $('diag-ports');
  if (pbox) {
    if (!po.length) {
      pbox.innerHTML = '<div class="kvrow dim"><span>没有数据</span></div>';
    } else {
      const labels = {
        53: 'DNS', 443: 'HTTPS 中继', 1935: '收流', 6667: '弹幕', 5000: '界面',
      };
      pbox.innerHTML = po.map((x) => {
        const name = labels[x.port] || '管理口';
        const ok = !!x.listening;
        const who = x.process ? (' · ' + esc(x.process)) : '';
        return '<div class="kvrow"><span>' + x.port + ' ' + name + who
          + '</span><b class="' + (ok ? 'ok' : 'err') + '">'
          + (ok ? '已开' : '未开') + '</b></div>';
      }).join('');
    }
  }

  // 防火墙明细（容器里没有 Windows 防火墙这回事，整块不显示）
  const fbox = $('diag-fw');
  if (fbox && isDocker()) {
    const wrap = fbox.closest('.diagbox') || fbox.parentElement;
    if (wrap) wrap.style.display = 'none';
    const t = $('diag-ports-title');
    if (t) t.textContent = '端口';
  } else if (fbox) {
    const keys = Object.keys(fw);
    if (!keys.length) {
      fbox.innerHTML = '<div class="kvrow dim"><span>没有数据</span></div>';
    } else {
      const miss = keys.filter((k) => fw[k] !== true);
      const head = '<div class="kvrow"><span>规则放行</span><b class="'
        + (miss.length ? 'warn' : 'ok') + '">'
        + (keys.length - miss.length) + '/' + keys.length + '</b></div>';
      const lines = miss.length
        ? miss.map((k) => '<div class="kvrow"><span>&nbsp;&nbsp;缺：' + esc(k)
            + '</span><b class="warn">未放行</b></div>').join('')
        : '<div class="kvrow"><span>&nbsp;&nbsp;全部已放行</span><b class="ok">✓</b></div>';
      fbox.innerHTML = head + lines;
    }
  }

  const pub = (L.nginx || {}).publisher || {};
  if (pub.present) {
    const q = pub.width ? (pub.width + 'x' + pub.height + ' ' + Math.round((pub.kbps || 0) / 1000) + 'M') : '';
    rows.push(['推流会话', '有（已 ' + pub.uptime + 's）' + (q ? ' ' + q : ''), 'ok']);
  } else {
    rows.push(['推流会话', '无', '']);
  }

  // ── 系统自检：通用项在前，三个方案各自的条件在后 ──
  const env = L.env || {};
  const sec = L.security || {};
  const ck = L.checks || {};
  const accel = L.accel || [];
  const portOn = (p) => {
    const it = ((L.ports || {}).items || []).find((x) => Number(x.port) === Number(p));
    return !!(it && it.listening);
  };
  const ps5row = ['PS5 流量', L.ps5_seen ? '已到达' : '还没收到',
    L.ps5_seen ? 'ok' : ''];

  rows.push(['__group__', '通用 · 三种方案都要看']);
  rows.push(['网络环境', env.detail || '未判定', env.level === 'ok' ? 'ok' : (env.level || '')]);
  const rt = L.router || {};
  const capTxt = { yes: '（能改 hosts）', hard: '（有 hosts 但很麻烦）',
                   no: '（没有 hosts）', oem: '（光猫，改不了）' }[rt.hosts] || '';
  rows.push(['路由器',
    rt.brand ? (rt.brand + (rt.model ? ' ' + rt.model : '') + capTxt)
             : (rt.reachable ? '后台能打开，型号没认出来' : '没连上后台页面'),
    rt.hosts === 'yes' ? 'ok' : (rt.brand ? 'warn' : '')]);
  rows.push(['本机 IP', d.lan_ip || '—', '']);
  rows.push(['网关', d.gateway || (env.gateway || '—'), '']);
  // 管理员权限和安全软件都是 Windows 那套才有的东西。容器里 is_admin 看的是
  // euid，跟“要不要点右键以管理员运行”完全没关系，拿来当检查项只会误导。
  const isWin = CAPS.platform === 'windows';
  if (isWin) {
    rows.push(['管理员权限', ck.admin ? '是' : '否', ck.admin ? 'ok' : 'err']);
    rows.push(['安全软件', (sec.found || []).length ? sec.found.join('、') : '未发现',
      sec.level === 'warn' ? 'warn' : 'ok']);
  }
  rows.push(['1935 收流', portOn(1935) ? '在听' : '没在听', portOn(1935) ? 'ok' : 'err']);
  rows.push(['6667 弹幕', portOn(6667) ? '在听' : '没在听', portOn(6667) ? 'ok' : 'err']);
  rows.push(['收流进程', ck.rtmp_proc || '未识别到', ck.rtmp_proc ? 'ok' : 'err']);
  rows.push(['ffmpeg 转发', ck.ffmpeg ? '在跑' : '未启动', ck.ffmpeg ? 'ok' : '']);
  rows.push(['直播阶段', L.live_state || 'idle', '']);
  if (d.version) rows.push(['版本', 'v' + d.version + ' · ' + (d.platform || ''), '']);
  rows.push(ps5row);

  rows.push(['__group__', '方案一 · 路由器 hosts']);
  // 443 中继只在方案二（内置 DNS）启动，容器里更是压根没有，别列出来吓人
  if (!isDocker()) {
    rows.push(['443 中继', L.relay_443 ? '运行中' : '未开', L.relay_443 ? 'ok' : '']);
    rows.push(['Twitch 代理', L.proxy_set ? '已配置' : '未配置',
      L.proxy_set ? 'ok' : 'warn']);
  }
  rows.push(['PS5 设置', '保持「自动获取」', '']);

  if (CAPS.builtin_dns) {
    rows.push(['__group__', '方案二 · 内置 DNS']);
    rows.push(['DNS 服务', (L.dns || {}).running ? '运行中' : '未开',
      (L.dns || {}).running ? 'ok' : '']);
    rows.push(['53 端口', portOn(53) ? '在听' : '未开或被别的程序占用',
      portOn(53) ? 'ok' : 'warn']);
  }

  if (CAPS.divert) {
    rows.push(['__group__', '方案三 · 流量接管']);
    rows.push(['加速器', accel.length ? accel.join('、') : '没检测到进程',
      accel.length ? 'ok' : 'warn']);
    rows.push(['WinDivert', (L.divert || {}).running ? '已加载' : '未加载',
      (L.divert || {}).running ? 'ok' : '']);
  }

  if ($('diag-local')) $('diag-local').innerHTML = kvRows(rows);

  const box = $('diag-issues');
  if (box) {
    const list = d.issues || [];
    box.innerHTML = list.length
      ? list.map((i) => '<li class="' + esc(i.level) + '">' + esc(i.msg) + '</li>').join('')
      : '<li class="ok">没发现明显问题。</li>';
  }

  if (bar) {
    bar.className = 'diagbar ' + (d.level || '');
    bar.textContent = d.verdict || '';
  }

  queryTwitchDiag();
}

async function queryTwitchDiag() {
  const box = $('diag-twitch');
  if (!box) return;
  box.innerHTML = '<div class="kvrow"><span>状态</span><b>查询中…</b></div>';

  let t = null;
  try { t = await api('/api/twitch/session'); } catch (e) {}
  if (!t) {
    box.innerHTML = '<div class="kvrow"><span>状态</span><b class="err">查询失败</b></div>';
    return;
  }

  const rows = [];
  if (!t.channel) {
    rows.push(['频道', '没填', 'warn']);
    box.innerHTML = kvRows(rows)
      + '<div class="kvrow"><span>说明</span><b style="font-family:inherit">第 4 步填上 Twitch 频道名，这里才能查</b></div>';
    return;
  }

  rows.push(['频道', t.channel, '']);
  if (t.verdict === 'live') rows.push(['当前', '直播中', 'warn']);
  else if (t.verdict === 'stale') rows.push(['当前', '未在播（刚播过）', 'warn']);
  else if (t.verdict === 'offline') rows.push(['当前', '未在播', 'ok']);
  else rows.push(['当前', '查不到', 'err']);

  const lb = t.last_broadcast || {};
  if (lb.started_at) {
    rows.push(['上次直播', fmtTs(lb.ts) + '  ' + String(lb.title || '').slice(0, 14), '']);
  }
  rows.push(['历史录像', (t.vod_count || 0) > 0 ? t.vod_count + ' 个' : '无',
    (t.vod_count || 0) > 0 ? 'ok' : '']);
  if (t.proxy) rows.push(['走的是', String(t.proxy).replace(/^https?:\/\//, ''), '']);
  rows.push(['耗时', (t.ms || 0) + 'ms', '']);

  box.innerHTML = kvRows(rows);
  if (t.advice) {
    box.innerHTML += '<div class="kvrow"><span>建议</span><b style="font-family:inherit;font-size:14px;line-height:1.5">'
      + esc(t.advice) + '</b></div>';
  }
}

let _sniffTimer = null;

async function toggleSniff() {
  const btn = $('sniff-btn');
  if (!btn) return;

  if (btn.dataset.on === '1') {
    if (_sniffTimer) clearInterval(_sniffTimer);
    await api('/api/sniff/stop', {});
    btn.dataset.on = '0';
    btn.textContent = '开始嗅探（30 秒）';
    renderSniff(await api('/api/sniff/status'));
    return;
  }

  btn.disabled = true;
  btn.textContent = '启动中…';
  const r = await api('/api/sniff/start', { seconds: 30 });
  btn.disabled = false;
  if (!r || r.ok === false) {
    if ($('sniff-msg')) $('sniff-msg').textContent = (r && r.error) || '嗅探起不来';
    btn.textContent = '开始嗅探（30 秒）';
    return;
  }

  btn.dataset.on = '1';
  btn.textContent = '停止';
  renderSniff(r);
  _sniffTimer = setInterval(async () => {
    const st = await api('/api/sniff/status');
    if (!st) return;
    renderSniff(st);
    if (!st.running) {
      clearInterval(_sniffTimer);
      btn.dataset.on = '0';
      btn.textContent = '再嗅一次';
    }
  }, 1000);
}

function renderSniff(st) {
  const box = $('sniff-result');
  if (!box || !st) return;

  const msg = $('sniff-msg');
  if (msg) {
    msg.textContent = st.running
      ? ('剩 ' + st.remain + ' 秒 · 已抓 ' + st.total + ' 个连接 · ' + (st.ps5_ip || '自动识别'))
      : '';
  }

  if (!st.ports || !st.ports.length) {
    box.innerHTML = st.verdict
      ? '<div class="diagbar ' + (st.level || '') + '" style="margin-top:10px">' + esc(st.verdict) + '</div>'
      : '';
    return;
  }

  let html = '<div class="diagbox" style="margin-top:10px">';
  st.ports.forEach((p) => {
    const cls = (p.alt && p.remote) ? 'err' : (p.local ? 'ok' : 'warn');
    const dest = p.dst && p.dst.length ? p.dst.join(', ') : '本机';
    html += '<div class="kvrow"><span>' + p.port + '</span><b class="' + cls + '">'
      + p.count + ' 次 → ' + esc(dest) + (p.alt ? '  ⚠ 备用端口' : '') + '</b></div>';
  });
  html += '</div>';
  if (st.verdict) {
    html += '<div class="diagbar ' + (st.level || '') + '">' + esc(st.verdict) + '</div>';
  }
  box.innerHTML = html;
}

async function exportDiag() {
  const msg = $('diag-export-msg');
  if (msg) msg.textContent = '正在打包…';
  try {
    const r = await api('/api/diag/export', {});
    if (r && r.ok && r.url) {
      window.open(r.url, '_blank');
      if (msg) msg.textContent = '已生成：' + r.name;
    } else {
      if (msg) msg.textContent = '导出失败：' + ((r && r.msg) || '未知错误');
    }
  } catch (e) {
    if (msg) msg.textContent = '导出失败：' + e;
  }
}

function applyDebugBtn(on) {
  const btn = $('log-debug-btn');
  if (!btn) return;
  btn.classList.toggle('on', !!on);
  btn.textContent = on ? '调试 开' : '调试';
  btn.title = on
    ? '正在记录详细日志（会写很多行），排查完记得关掉'
    : '打开后记录中继连接、嗅探包、更新比对等详细日志';
}

async function loadRtmpImpl() {
  try {
    const r = await api('/api/rtmp_impl');
    const sel = $('rtmp-impl');
    const hint = $('impl-hint');
    if (sel && r) sel.value = r.impl || 'nginx';
    if (hint) {
      hint.textContent = r && r.impl === r.active
        ? '当前生效'
        : '下次启动生效（正在直播时切换会这样）';
    }
  } catch (e) {}
}

async function setRtmpImpl(v) {
  const hint = $('impl-hint');
  if (hint) hint.textContent = '正在切换…';
  const r = await api('/api/rtmp_impl', { impl: v });
  if (!r || !r.ok) {
    if (hint) hint.textContent = (r && (r.error || r.detail)) || '切换失败';
    return;
  }
  if (hint) {
    hint.textContent = r.need_restart
      ? (r.detail || '已保存，重启程序后生效')
      : '已生效';
  }
  await tick();
}

async function toggleDebugLog() {
  const cur = await api('/api/logs/debug');
  const next = !(cur && cur.on);
  const r = await api('/api/logs/debug', { on: next });
  applyDebugBtn(r && r.on);
  if (!next) {
    tick();
  }
}

function copyQq(el) {
  const num = '1092104797';
  const flash = () => {
    const old = el.textContent;
    el.textContent = '已复制';
    setTimeout(() => { el.textContent = old; }, 1200);
  };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = num;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); flash(); } catch (e) {}
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(num).then(flash).catch(fallback);
  } else {
    fallback();
  }
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
  // 方案一用不上端口接管和内置 DNS，点下去就把它们停掉。
  // 以前这里什么都不做，界面写着方案一、后台还在拦包，重启还会自动开回来。
  const hj = (S.state && S.state.hijack) || {};
  if (hj.divert && hj.divert.running) await api('/api/divert/stop', {});
  if (hj.dns_running) await api('/api/builtin/stop', {});
  $('s2-pick-note').innerHTML = 'PS5 保持「自动获取」，什么都不用改。<br>'
    + '本机不接管、不代理 DNS，PS5 直连这台电脑的收流端口。<br>'
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


  const dnsMode = (S.state && S.state.dns_mode) || 'router';
  const _pk3 = S.modePick
    || (((S.state && S.state.dns_mode) === 'builtin') ? 'dns' : '');
  const routerDone = (_pk3 === 'hosts')
    || (!S.modePick && !(dv.running) && !(S.state && S.state.hijack && S.state.hijack.dns_running)
        && !!(S.state && S.state.progress_dns_ok));
  if (accel.length && _pk3 !== 'hosts') {
    // 方案一走路由器 hosts，本机装没装加速器跟它没关系，别在这里误导
    $('s3-badge').textContent = '加速器在跑';
    $('s3-badge').className = 'badge ok';
    $('s3-accel').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>本机加速器：${esc(accel.join('、'))}</h4>
        <p>确认它在给 PS5 加速。它给的四个数，下面照抄。</p>
        </div></div>`;
  } else if (dnsMode === 'builtin' || _pk3 === 'dns') {
    // 方案二：PS5 直接把 DNS 指到本机，压根不需要加速器
    $('s3-badge').textContent = '方案二 · 不用加速器';
    $('s3-badge').className = 'badge ok';
    $('s3-accel').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>这个方案不需要本机加速器</h4>
        <p>你用的是<b>内置 DNS</b> 方案：PS5 直接把这台电脑当 DNS，
        跟加速器没有关系，本机装没装加速器都一样。</p>
        <p>这页不用填，直接去第 4 步。</p>
        </div></div>`;
  } else if (_pk3 === 'divert'
             || (!_pk3 && (S.state && S.state.hijack || {}).mode === 'divert')) {
    // 方案三：加速器必须在**这台电脑**上，不可能在路由器上
    $('s3-badge').textContent = '没找到加速器';
    $('s3-badge').className = 'badge warn';
    $('s3-accel').innerHTML = `<div class="verdict warn"><div class="icon">!</div><div>
        <h4>没检测到本机加速器进程</h4>
        <p>你选的是<b>流量接管</b>（方案三），它要求加速器装在<b>这台电脑</b>上，
        由它把 PS5 的流量引到电脑来。加速器装在路由器上是不成立的做法。</p>
        <p>请先打开电脑上的加速器、确认它正在加速 PS5，然后点下面的
        「检测加速器」刷新。四个数照它显示的抄。</p>
        </div></div>`;
  } else if (routerDone) {
    // 方案一靠路由器 hosts，跟本机有没有加速器无关，别假设人家装了加速器
    $('s3-badge').textContent = '方案一 · 不用填';
    $('s3-badge').className = 'badge ok';
    $('s3-accel').innerHTML = `<div class="verdict ok"><div class="icon">✓</div><div>
        <h4>方案一：PS5 什么都不用改</h4>
        <p>这个方案靠<b>路由器的 hosts</b> 把推流域名指到这台电脑，
        跟电脑上装没装加速器没有关系。</p>
        <p>PS5 保持「自动获取」就行，下面这几个数一个都不用填。</p>
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
    const dm = (S.state && S.state.dns_mode) || 'router';
    $('s3-fillnote').textContent = (dm === 'builtin')
      ? '方案二不用填 PS5 的网络参数，PS5 保持自动获取就行。'
      : '本机没找到加速器，四个数请照加速器界面手动抄。';
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

let _ncAt = 0;

async function loadNetcheck(force) {
  if (!force && Date.now() - _ncAt < 60000) return;
  _ncAt = Date.now();
  let d = null;
  try { d = await api('/api/netcheck' + (force ? '?force=1' : '')); } catch (e) {}
  if (!d) return;

  const v = d.version || {};
  if ($('rail-ver-latest')) {
    if (v.latest) {
      const same = v.is_latest === true;
      $('rail-ver-latest').textContent = same ? ('v' + v.latest + ' 已最新')
        : ('v' + v.latest + ' 可更新');
      $('rail-ver-latest').className = 'rail-v ' + (same ? 'ok' : 'warn');
    } else {
      $('rail-ver-latest').textContent = '查不到';
      $('rail-ver-latest').className = 'rail-v';
    }
  }

  const px = d.proxy || {};
  if ($('rail-proxy')) {
    if (px.configured) {
      const shown = px.configured.replace(/^https?:\/\//, '');
      // 列宽有限，只显示状态，完整地址挂在 title 上
      $('rail-proxy').textContent = px.ok ? ('已连 ' + px.ms + 'ms') : '不通';
      $('rail-proxy').title = shown;
      $('rail-proxy').className = 'rail-v ' + (px.ok ? 'ok' : 'err');
    } else {
      $('rail-proxy').textContent = '未配置';
      $('rail-proxy').className = 'rail-v';
    }
  }

  const tw = d.twitch || {};
  if ($('rail-twitch')) {
    $('rail-twitch').textContent = tw.ok ? ('通 ' + tw.ms + 'ms') : '不通';
    $('rail-twitch').className = 'rail-v ' + (tw.ok ? 'ok' : 'err');
  }

  const gh = d.github || {};
  if ($('rail-github')) {
    $('rail-github').textContent = gh.ok ? ('通 ' + gh.ms + 'ms') : '不通';
    $('rail-github').className = 'rail-v ' + (gh.ok ? 'ok' : 'err');
  }
}

function renderOutputMode() {
  const st = S.state || {};
  const live = st.live || {};
  const outs = st.outputs || {};
  renderOutputs(outs);

  const box = $('s5-local');
  if (!box) return;
  if (!outs.local_stream) { box.innerHTML = ''; return; }

  const url = live.local_url || '';
  const ready = !!url;
  box.innerHTML = `
    <div class="urlbox">
      <span class="mono" id="s5-url">${esc(url || '等 PS5 开始直播后出现地址')}</span>
      ${ready ? '<button class="btn sm" onclick="copyUrl()">复制</button>' : ''}
    </div>
    <p class="small dim">用 OBS 的「媒体源」，或 PotPlayer / VLC 打开这个地址就能本地看，不影响推 B 站。</p>
  `;
}

function copyUrl() {
  const el = $('s5-url');
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    el.textContent = '已复制';
    setTimeout(renderOutputMode, 1200);
  });
}


async function setOutputs() {
  const push = $('sw-push') && $('sw-push').checked;
  const local = $('sw-local') && $('sw-local').checked;
  const r = await api('/api/outputs', { push_bili: push, local_stream: local });
  if (!r || !r.ok) { alert((r && r.msg) || '保存失败'); return; }
  await tick();
}

function renderOutputs(o) {
  if (!o) return;
  const a = $('sw-push'), b = $('sw-local');
  if (a) a.checked = !!o.push_bili;
  if (b) b.checked = !!o.local_stream;
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

async function restartApp() {
  if (isDocker()) {
    alert('容器里没有「重启」按钮。\n重启容器：docker compose restart');
    return;
  }
  if (!confirm('重启会中断当前的直播/本地取流，然后自动重新打开。\n确定重启？')) return;
  await api('/api/restart', {});
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;'
    + 'color:#8b93a7;font-family:system-ui"><div style="text-align:center;line-height:1.9">'
    + '<div style="font-size:16px;margin-bottom:6px">正在重启…</div>'
    + '<div style="font-size:13px">3 秒后自动开回来。要是没反应，手动双击一下程序。</div></div></div>';
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
    } else if (r.scope === 'web') {
      const kb = Math.round((r.web_size || 0) / 1024);
      showUpdateBar(
        `发现新版本 v${ver}（只有界面文件变了，要下载 ${kb} KB）`,
        '立即更新', doUpdate);
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
    } else if (s.state === 'ready' && s.message && s.message.indexOf('界面') >= 0) {
      clearInterval(timer);
      b.textContent = '已更新';
      b.disabled = true;
      $('update-msg').textContent = '界面文件已更新，关掉程序重新打开后生效';
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


  // 界面适配失败不能连累后面的 goStep，否则整页空白，还看不出为什么。
  // 出错就把原因贴到页面顶部，截图和远程排障都能直接看到。
  try {
    applyCaps(st);
  } catch (e) {
    console.error('applyCaps 失败', e);
    document.body.insertAdjacentHTML('afterbegin',
      '<pre id="boot-err" style="color:#f66;background:#2a0000;padding:12px;'
      + 'font-size:13px;white-space:pre-wrap;margin:0">applyCaps 出错：'
      + (e && e.message ? e.message : e) + '</pre>');
  }


  const q = new URLSearchParams(location.search);
  const forced = Number(q.get('step') || 0);
  let step = forced || Number((st && st.step) || 1);
  if (step < 1 || step > 5) step = 1;
  // 支持 ?step=5 / ?step=diag 直接定位（截图和排障时用）
  const _qs = new URLSearchParams(location.search).get('step');
  if (_qs) {
    goStep(isNaN(Number(_qs)) ? _qs : Number(_qs), true);
  } else if (isDocker()) {
    // 容器版没有「启动时用浏览器打开」这步，用户是直接访问根路径的，
    // 所以在这里也落到系统自检页，行为跟桌面版对齐：先看这台机器行不行。
    goStep('info', true);
  } else {
    goStep(step, true);
  }

  tick();
  // 优先用 SSE 长连接：一条连接持续推状态，不再每 1.5 秒新建 TCP
  if (window.EventSource) {
    try {
      const es = new EventSource('/api/events');
      es.onmessage = (ev) => {
        try { applyState(JSON.parse(ev.data)); } catch (e) {}
      };
      es.onerror = () => {};   // 断了浏览器会自动重连，不用管
      S._es = es;
    } catch (e) {
      setInterval(tick, 1500);
    }
  } else {
    setInterval(tick, 1500);
  }


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


async function checkProxy(which) {
  const first = which === '1st';
  const inp = first ? $('in-proxy-1st') : $('in-proxy');
  const btn = first ? $('btn-proxy-1st') : $('btn-proxy-4');
  const box = first ? $('prox-msg-1st') : $('prox-msg-4');
  if (!inp || !box) return;

  const val = (inp.value || '').trim();
  const label = btn ? btn.textContent : '检测';
  if (btn) { btn.disabled = true; btn.textContent = '检测中…'; }
  box.className = 'proxmsg on';
  box.textContent = val ? '正在测试这个代理…' : '正在测试直连，并检查本机代理…';

  try {
    const r = await api('/api/proxy/check', { proxy: val });
    const v = (r && r.verdict) || 'error';
    let cls = 'ok';
    if (v === 'none' || v === 'proxy_bad' || v === 'error') cls = 'err';
    else if (v === 'local_ok') cls = 'warn';
    box.className = 'proxmsg on ' + cls;
    box.innerHTML = esc((r && r.advice) || '检测完成');
    if (!val && r && r.chosen) {
      box.innerHTML += '<br>实际会用：<b>' + esc(r.chosen) + '</b>';
    }
  } catch (e) {
    box.className = 'proxmsg on err';
    box.textContent = '检测失败：' + (e && e.message ? e.message : e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function saveProxyAndStartDns() {
  const el = $('in-proxy-1st');
  const proxy = (el ? el.value : '').trim();

  if (proxy && !/^(https?:\/\/)?[\w.-]+:\d+/.test(proxy)) {
    alert('代理格式看起来不对。\n格式是 http://主机:端口，例如 http://192.168.1.100:7890');
    if (el) el.focus();
    return;
  }

  if (!proxy) {
    const btn = $('btn-proxy-1st');
    const box = $('prox-msg-1st');
    const label = btn ? btn.textContent : '检测';
    if (btn) { btn.disabled = true; btn.textContent = '检测中…'; }
    if (box) { box.className = 'proxmsg on'; box.textContent = '正在确认本机有没有能上外网的通道…'; }
    let ck = null;
    try { ck = await api('/api/proxy/check', { proxy: '' }); } catch (e) {}
    if (btn) { btn.disabled = false; btn.textContent = label; }

    if (!ck) {
      alert('检测没拿到结果，请重试，或直接填一个代理地址。');
      return;
    }
    const ok = ck.verdict === 'direct_ok' || ck.verdict === 'local_ok';
    if (box) {
      box.className = 'proxmsg on ' + (ok ? (ck.verdict === 'local_ok' ? 'warn' : 'ok') : 'err');
      box.innerHTML = esc(ck.advice || '');
      if (ok && ck.chosen) box.innerHTML += '<br>实际会用：<b>' + esc(ck.chosen) + '</b>';
    }
    if (!ok) {
      alert('留空需要有能上外网的通道，但现在直连和本机代理都不通。\n\n'
        + (ck.advice || '') + '\n\n请在上面填一个代理地址，或先打开 Clash / mihomo。');
      return;
    }
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

async function manualStart() {
  const r = await api('/api/live/start', {});
  if (!r.ok) alert(r.msg || '开播失败');
  tick();
}

async function setOutputMode(mode) {
  const r = await api('/api/output/mode', { mode });
  if (r && !r.ok) { alert('切换失败：' + (r.msg || '未知原因')); return; }
  await tick();
  renderOutputMode();
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
