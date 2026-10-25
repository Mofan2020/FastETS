/* FastETS front-end (Liquid Glass) */
'use strict';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  ready: false,
  settings: {},
  homework: [],
  selected: new Set(),
  papers: {},
  seq: 0,
  polling: false,
  tab: 'account',
};

const PAGE_TITLES = {
  account: '账号', homework: '作业', voice: '声音', settings: '设置',
};

/* ------------------------------------------------------------------ utils */
function formatValue(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }
  return String(value);
}

function log(message, level = 'info') {
  const body = $('#log-body');
  const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const text = formatValue(message);
  body.textContent += `[${stamp}] ${text}\n`;
  body.scrollTop = body.scrollHeight;
  if (body.classList.contains('hidden')) body.classList.remove('hidden');
  $('#log-title').textContent = text;
}

function setStatus(text) { $('#status-text').textContent = text; }

function setProgress(percent) {
  const fill = $('#progress-fill');
  fill.style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
}

async function api(method, ...args) {
  if (!state.ready) return { ok: false, message: '应用尚未就绪' };
  try {
    const fn = window.pywebview.api[method];
    if (!fn) return { ok: false, message: `接口 ${method} 不存在` };
    return await fn(...args);
  } catch (err) {
    const message = (err && err.message) || String(err);
    log(`调用 ${method} 失败：${message}`, 'error');
    return { ok: false, message };
  }
}

function toast(message, kind = 'error') {
  log(message, kind);
  if (kind === 'error') {
    $('#log-toggle').click();
  }
}

/* ------------------------------------------------------------------- tabs */
function showTab(name) {
  state.tab = name;
  $$('.nav-item').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === name));
  $$('.page').forEach((page) => page.classList.toggle('active', page.dataset.page === name));
  $('#page-title').textContent = PAGE_TITLES[name] || name;
  if (name === 'homework' && !state.homework.length) loadHomework();
}

$$('.nav-item').forEach((btn) => btn.addEventListener('click', () => showTab(btn.dataset.tab)));

/* ------------------------------------------------------------- disclaimer */
function showDisclaimer(text, title) {
  $('#disclaimer-text').textContent = text || '';
  $('#disclaimer-title').textContent = title || '使用前必读 · 免责声明';
  $('#disclaimer').classList.remove('hidden');
}

$('#disclaimer-accept').addEventListener('click', async () => {
  await api('accept_disclaimer');
  $('#disclaimer').classList.add('hidden');
  log('已接受免责声明，请正常使用本工具并保证真实练习。');
});

$('#disclaimer-reject').addEventListener('click', () => {
  window.close();
});

$('#show-disclaimer').addEventListener('click', async () => {
  const boot = await api('boot');
  showDisclaimer(boot.disclaimer_text, boot.disclaimer_title);
});

/* ------------------------------------------------------------------ login */
$('#btn-login').addEventListener('click', async () => {
  const phone = $('#login-phone').value.trim();
  const password = $('#login-password').value;
  if (!phone || !password) { toast('请填写手机号与密码'); return; }
  setStatus('登录中…');
  const result = await api('login', phone, password, $('#login-remember').checked);
  if (!result.ok) { setStatus('未登录'); toast(result.message); return; }

  if (result.need_account) {
    openAccountPicker(result.accounts || []);
    return;
  }
  afterLogin(result);
});

function openAccountPicker(accounts) {
  const list = $('#account-list');
  list.innerHTML = '';
  accounts.forEach((account) => {
    const item = document.createElement('div');
    item.className = 'account-item';
    item.innerHTML = `<b>${escapeHtml(account.grade || '')} ${escapeHtml(account.name || '')}</b>
                      <span>${escapeHtml(account.class_name || '')} · 账号 ${escapeHtml(account.id || '')}</span>`;
    item.addEventListener('click', async () => {
      const result = await api('select_account', account.id);
      if (!result.ok) { toast(result.message); return; }
      $('#account-picker').classList.add('hidden');
      afterLogin(result);
    });
    list.appendChild(item);
  });
  $('#account-picker').classList.remove('hidden');
}

$('#account-cancel').addEventListener('click', () => {
  $('#account-picker').classList.add('hidden');
  setStatus('未登录');
});

function afterLogin(result) {
  setStatus('已登录');
  $('#side-account').textContent = `${result.grade || ''}${result.account || ''}`.trim() || '已登录';
  $('#login-password').value = '';
  log(`登录成功：${result.grade || ''} ${result.account || ''}`);
  showTab('homework');
}

$('#btn-logout').addEventListener('click', async () => {
  await api('logout');
  setStatus('未登录');
  $('#side-account').textContent = '—';
  log('已退出登录');
});

$('#btn-import-token').addEventListener('click', async () => {
  const token = $('#token-input').value.trim();
  if (!token) { toast('请填写 token'); return; }
  const result = await api('import_token', token);
  if (!result.ok) { toast(result.message); return; }
  afterLogin(result);
});

$('#btn-changyan').addEventListener('click', async () => {
  const result = await api('changyan_login_start');
  if (!result.ok) { toast(result.message); return; }
  log('畅言登录窗口已打开，请在弹出的窗口中完成登录…');
  pollChangyan();
});

async function pollChangyan() {
  const result = await api('changyan_login_poll');
  if (!result.ok) { toast(result.message); return; }
  if (result.need_account) {
    openAccountPicker(result.accounts || []);
    return;
  }
  if (result.logged_in) {
    setStatus('已登录');
    log('畅言登录成功');
    showTab('homework');
    return;
  }
  setTimeout(pollChangyan, 1500);
}

/* --------------------------------------------------------------- homework */
async function loadHomework() {
  const status = $('#homework-status').value;
  setStatus('拉取作业中…');
  const result = await api('list_homework', status);
  if (!result.ok) { setStatus('拉取失败'); toast(result.message); return; }
  state.homework = (result.result && result.result.homeworks) || [];
  state.selected.clear();
  renderHomework();
  setStatus('已登录');
  log(`拉取到 ${state.homework.length} 个作业`);
}

const STATE_LABELS = { 1: '未完成', 2: '进行中', 3: '已完成' };

function renderHomework() {
  const box = $('#homework-list');
  box.innerHTML = '';
  if (!state.homework.length) {
    box.innerHTML = '<p class="empty">没有符合条件的作业</p>';
    return;
  }
  state.homework.forEach((hw, index) => {
    const item = document.createElement('div');
    item.className = 'list-item';
    const parts = (hw.contents || []).length;
    const groups = [...new Set((hw.contents || []).map((c) => c.group_name))].filter(Boolean);
    const status = hw.status || '1';
    const statusTag = status === '3'
      ? '<span class="tag ok">已完成</span>'
      : (status === '2' ? '<span class="tag warn">进行中</span>' : '<span class="tag">未完成</span>');
    item.innerHTML = `
      <input type="checkbox" data-index="${index}" />
      <div class="list-main">
        <div class="list-title">${statusTag}${escapeHtml(hw.name || '未命名作业')}</div>
        <div class="list-meta">
          ${groups.map((g) => `<span class="tag">${escapeHtml(g)}</span>`).join('')}
          <span>${parts} 个资源包</span>
          ${hw.parent_name ? `<span>来自作业集「${escapeHtml(hw.parent_name)}」</span>` : ''}
        </div>
      </div>
      <button class="btn btn-secondary redo-one">重做</button>`;
    const checkbox = $('input', item);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(index); else state.selected.delete(index);
    });
    const redoBtn = $('.redo-one', item);
    redoBtn.addEventListener('click', async () => {
      await runBatch([hw], true);
    });
    box.appendChild(item);
  });
}

$('#btn-load-homework').addEventListener('click', loadHomework);
$('#homework-status').addEventListener('change', loadHomework);

function selectedHomeworks() {
  return Array.from(state.selected).map((i) => state.homework[i]).filter(Boolean);
}

$('#btn-inspect').addEventListener('click', async () => {
  const picked = selectedHomeworks();
  if (!picked.length) { toast('请先勾选要解析的作业'); return; }
  const box = $('#paper-view');
  box.innerHTML = '<p class="empty">解析中…</p>';
  for (const hw of picked) {
    const result = await api('load_paper', hw);
    if (!result.ok) { box.innerHTML = `<p class="empty">解析失败：${escapeHtml(result.message)}</p>`; continue; }
    state.papers[hw.id] = result.paper;
  }
  renderPapers();
});

function renderPapers() {
  const box = $('#paper-view');
  const papers = Object.values(state.papers);
  if (!papers.length) {
    box.innerHTML = '<p class="empty">解析后在这里显示每个大题的题型与朗读文本</p>';
    return;
  }
  box.innerHTML = '';
  papers.forEach((paper) => {
    const wrap = document.createElement('div');
    wrap.className = 'paper-section';
    (paper.sections || []).forEach((section) => {
      const head = document.createElement('div');
      head.className = 'paper-section-head';
      head.innerHTML = `<span>${escapeHtml(section.group_name || '')}</span>
                        <span>${escapeHtml(section.structure_type || '')} · ${(section.questions || []).length} 题</span>`;
      wrap.appendChild(head);
      (section.questions || []).forEach((q) => {
        const row = document.createElement('div');
        row.className = 'paper-q';
        const candidates = q.answer_candidates || [];
        let segments;
        if (candidates.length > 1) {
          // 复述 / 表达：多份示例答案，朗读时只会挑其中一份。
          segments = `共 ${candidates.length} 份示例答案，朗读时按设置挑选（${escapeHtml(pickModeLabel())}）`;
        } else if (candidates.length === 1) {
          segments = candidates[0];
        } else {
          segments = (q.answers && q.answers.length) ? q.answers.join(' / ') : (q.read_text || '');
        }
        row.innerHTML = `<div class="paper-q-text">第 ${q.order} 题 · ${escapeHtml(q.kind || '')} · 满分 ${q.max_score}</div>
                         <div class="paper-q-body">${escapeHtml(segments)}</div>`;
        wrap.appendChild(row);
      });
    });
    box.appendChild(wrap);
  });
}

async function runBatch(picked, redo) {
  if (!picked || !picked.length) { toast('请先勾选要处理的作业'); return; }
  const dry = $('#set-dry-run').checked;
  const verb = redo ? '重做' : '提交';
  if (!dry && !window.confirm(`即将向易听说${verb}真实成绩，确认继续？\n（建议先在「设置」中开启演练模式验证流程）`)) return;
  const result = await api('start_complete', picked, dry, !!redo);
  if (!result.ok) { toast(result.message); return; }
  log(`已开始${dry ? '演练' : verb} ${picked.length} 个作业`);
}

$('#btn-start').addEventListener('click', () => runBatch(selectedHomeworks(), false));
$('#btn-redo').addEventListener('click', () => runBatch(selectedHomeworks(), true));

$('#btn-stop').addEventListener('click', async () => {
  const result = await api('stop_job');
  if (result.ok && result.stopped) log('已请求停止当前任务');
});

/* --------------------------------------------------------------- voice page */
function syncVoiceMode() {
  const mode = $('#voice-mode').value;
  $('#voice-clone-box').classList.toggle('hidden', mode !== 'clone');
  $('#voice-default-box').classList.toggle('hidden', mode !== 'default');
  const voiceId = state.settings.tts_provider === 'openai_compat'
    ? state.settings.openai_voice
    : state.settings.tts_provider === 'edge'
      ? state.settings.edge_voice
      : state.settings.minimax_voice_id;
  $('#side-voice').textContent = mode === 'default'
    ? '默认音色'
    : (voiceId ? '已克隆' : '未克隆');
}

$('#voice-mode').addEventListener('change', async () => {
  syncVoiceMode();
  await saveVoicePreferences();
});

$('#set-default-voice').addEventListener('change', () => { saveVoicePreferences(); });

async function saveVoicePreferences() {
  const settings = collectSettings();
  settings.voice_mode = $('#voice-mode').value;
  settings.default_voice = $('#set-default-voice').value.trim();
  const result = await api('save_settings', settings);
  if (!result.ok) { toast(result.message); return; }
  state.settings = result.settings;
  log(`声音设置已保存：${settings.voice_mode === 'default' ? '使用默认声音' : '克隆我的声音'}`);
}

$('#btn-load-voices').addEventListener('click', async () => {
  const button = $('#btn-load-voices');
  button.disabled = true;
  const result = await api('list_voices');
  button.disabled = false;
  if (!result.ok) { toast(result.message); return; }
  const voices = result.voices || [];
  const select = $('#voice-list');
  select.innerHTML = '<option value="">（选择一个音色）</option>';
  voices.forEach((voice) => {
    const option = document.createElement('option');
    option.value = voice.voice_id;
    option.textContent = `${voice.name || voice.voice_id} · ${voice.group || ''} · ${voice.voice_id}`;
    select.appendChild(option);
  });
  log(`已获取 ${voices.length} 个音色`);
});

$('#voice-list').addEventListener('change', async () => {
  const value = $('#voice-list').value;
  if (!value) return;
  $('#set-default-voice').value = value;
  await saveVoicePreferences();
});

/* ------------------------------------------------------------------ voice */
async function loadSamples() {
  const result = await api('voice_samples');
  if (!result.ok) return;
  $('#sample-zh').textContent = result.zh;
  $('#sample-en').textContent = result.en;
}

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

/*
 * Recording is deliberately not offered: the browser MediaRecorder inside
 * pywebview returned empty buffers on several platforms, so the sample has to
 * be recorded with an external recorder and imported as a file here.
 */
async function importRecording(file, kind) {
  if (!file) return;
  if (!file.size || file.size < 1024) {
    toast(`文件太小（${formatBytes(file.size)}），请导入有效的音频文件`);
    return;
  }
  setStatus('导入音频中…');
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  const result = await api('save_recording', btoa(binary), kind);
  setStatus('已导入');
  if (!result.ok) { toast(result.message); return; }
  const info = result.info || {};
  const parts = [
    `已导入${kind === 'zh' ? '中文' : '英文'}样本：${file.name}`,
    formatBytes(file.size),
    info.format || '未知格式',
  ];
  if (info.duration_ms) parts.push(`${(info.duration_ms / 1000).toFixed(2)}s`);
  if (info.sample_rate) parts.push(`${info.sample_rate} Hz`);
  if (typeof info.mean_volume_db === 'number') parts.push(`平均音量 ${info.mean_volume_db} dB`);
  log(parts.join(' · '));
  if (result.warning) toast(result.warning, 'warn');
}

$$('[data-import]').forEach((input) => {
  input.addEventListener('change', () => {
    importRecording(input.files && input.files[0], input.dataset.import);
    input.value = '';
  });
});

$$('[data-play]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const text = btn.dataset.play === 'zh' ? $('#sample-zh').textContent : $('#sample-en').textContent;
    if (!text) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = btn.dataset.play === 'zh' ? 'zh-CN' : 'en-US';
    speechSynthesis.speak(utterance);
  });
});

$('#btn-clone').addEventListener('click', async () => {
  const result = await api('clone_voice');
  if (!result.ok) { toast(result.message); return; }
  $('#voice-status').textContent = '克隆中…';
  log('开始克隆声音…');
});

$('#btn-test-tts').addEventListener('click', async () => {
  const result = await api('test_tts', '');
  if (!result.ok) { toast(result.message); return; }
  const blob = new Blob([Uint8Array.from(atob(result.audio), (c) => c.charCodeAt(0))], { type: result.mime });
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.play();
  $('#voice-status').textContent = '合成测试已播放';
});

/* --------------------------------------------------------------- settings */
const SETTING_FIELDS = [
  ['tts_provider', '#set-provider', 'str'],
  ['voice_mode', '#voice-mode', 'str'],
  ['default_voice', '#set-default-voice', 'str'],
  ['minimax_api_key', '#set-minimax-key', 'str'],
  ['minimax_base_url', '#set-minimax-base', 'str'],
  ['minimax_model', '#set-minimax-model', 'str'],
  ['minimax_language_boost', '#set-minimax-lang', 'str'],
  ['openai_base_url', '#set-openai-base', 'str'],
  ['openai_api_key', '#set-openai-key', 'str'],
  ['openai_model', '#set-openai-model', 'str'],
  ['openai_voice', '#set-openai-voice', 'str'],
  ['openai_clone_endpoint', '#set-openai-clone', 'str'],
  ['edge_voice', '#set-edge-voice', 'str'],
  ['macos_voice', '#set-macos-voice', 'str'],
  ['tts_fallback', '#set-tts-fallback', 'str'],
  ['answer_pick_mode', '#set-answer-pick', 'str'],
  ['tts_speed', '#set-speed', 'num'],
  ['tts_pitch', '#set-pitch', 'num'],
  ['score_mode', '#set-score-mode', 'str'],
  ['target_score_ratio', '#set-score-ratio', 'num'],
  ['dry_run', '#set-dry-run', 'bool'],
  ['pace_enabled', '#set-pace', 'bool'],
  ['request_delay_ms', '#set-delay', 'num'],
  ['request_jitter_ratio', '#set-jitter', 'num'],
  ['reading_wpm', '#set-wpm', 'num'],
  ['section_pause_ms', '#set-section-pause', 'num'],
  ['min_reading_delay_ms', '#set-min-read', 'num'],
  ['max_reading_delay_ms', '#set-max-read', 'num'],
  ['ffmpeg_path', '#set-ffmpeg', 'str'],
  ['redo_reset_route', '#set-redo-route', 'str'],
  ['skip_ssl_verify', '#set-skip-ssl', 'bool'],
  ['keep_audio_cache', '#set-keep-cache', 'bool'],
];

const MINIMAX_CN = 'https://api.minimaxi.com';
const MINIMAX_INTL = 'https://api.minimax.io';

function syncMinimaxRegion() {
  const region = $('#set-minimax-region').value;
  if (region === 'cn') $('#set-minimax-base').value = MINIMAX_CN;
  else if (region === 'intl') $('#set-minimax-base').value = MINIMAX_INTL;
}

function detectMinimaxRegion(baseUrl) {
  const url = String(baseUrl || '').trim();
  if (!url) return 'cn';
  if (url.includes('api.minimax.io')) return 'intl';
  if (url.includes('api.minimaxi.com')) return 'cn';
  return 'custom';
}

$('#set-minimax-region').addEventListener('change', syncMinimaxRegion);

function fillSettings(settings) {
  state.settings = settings || {};
  SETTING_FIELDS.forEach(([key, selector, type]) => {
    const element = $(selector);
    if (!element) return;
    if (type === 'bool') element.checked = Boolean(state.settings[key]);
    else if (type === 'num') element.value = state.settings[key] ?? 0;
    else element.value = state.settings[key] ?? '';
  });
  $('#ratio-value').textContent = Number(state.settings.target_score_ratio ?? 1).toFixed(2);
  const region = detectMinimaxRegion(state.settings.minimax_base_url);
  $('#set-minimax-region').value = region;
  if (region === 'custom') $('#set-minimax-base').value = state.settings.minimax_base_url || '';
  syncProviderBoxes();
  $('#side-mode').textContent = state.settings.dry_run ? '演练' : '真实提交';
  syncVoiceMode();
}

function collectSettings() {
  const out = {};
  SETTING_FIELDS.forEach(([key, selector, type]) => {
    const element = $(selector);
    if (!element) return;
    if (type === 'bool') out[key] = element.checked;
    else if (type === 'num') out[key] = Number(element.value || 0);
    else out[key] = element.value;
  });
  return out;
}

function syncProviderBoxes() {
  const provider = $('#set-provider').value;
  $$('.provider-box').forEach((box) => {
    box.classList.toggle('active', box.dataset.provider === provider);
  });
}

function pickModeLabel() {
  const select = $('#set-answer-pick');
  if (!select) return '默认读第一个';
  const option = select.options[select.selectedIndex];
  return option ? option.textContent : '默认读第一个';
}

$('#set-provider').addEventListener('change', syncProviderBoxes);
$('#set-score-ratio').addEventListener('input', (e) => {
  $('#ratio-value').textContent = Number(e.target.value).toFixed(2);
});

$('#btn-save-settings').addEventListener('click', async () => {
  const result = await api('save_settings', collectSettings());
  if (!result.ok) { toast(result.message); return; }
  fillSettings(result.settings);
  log('设置已保存');
});

$('#btn-check-env').addEventListener('click', async () => {
  const result = await api('check_environment');
  if (!result.ok) { toast(result.message); return; }
  log(`ffmpeg: ${result.ffmpeg || '未找到'}`);
  if (result.speex) log(`Speex 编码: ${result.speex.message || result.speex.method}`);
  log(`语音服务: ${(result.provider && result.provider.message) || '未知'}`);
});

/* ------------------------------------------------------------- device code */
async function loadDeviceCode() {
  const result = await api('boot');
  $('#device-code').textContent = result.device_code || '（登录后生成）';
}

$('#btn-copy-device').addEventListener('click', () => {
  const text = $('#device-code').textContent;
  navigator.clipboard.writeText(text).then(() => log('机器码已复制'));
});

/* ----------------------------------------------------------------- events */
async function pollEvents() {
  if (state.polling) return;
  state.polling = true;
  try {
    const result = await api('poll_events', state.seq);
    if (result.ok && result.events) {
      result.events.forEach((event) => {
        state.seq = Math.max(state.seq, event.seq + 1);
        handleEvent(event);
      });
    }
  } finally {
    state.polling = false;
  }
}

const STAGE_LABELS = {
  start: '开始', download: '下载', question: '题目', tts: '合成',
  upload: '上传', submit: '回传', clone: '克隆', done: '完成', error: '错误',
  choose: '选择', warn: '提示',
};

function handleEvent(event) {
  if (event.type === 'progress') {
    const stage = event.stage ? `[${STAGE_LABELS[event.stage] || event.stage}] ` : '';
    if (event.message) log(`${stage}${event.message}`);
    if (event.percent !== null && event.percent !== undefined) setProgress(event.percent);
    if (event.stage === 'choose' && event.choose) showAnswerPicker(event.choose);
  } else if (event.type === 'log') {
    log(event.message, event.level || 'info');
  } else if (event.type === 'job') {
    const job = event.job || {};
    if (job.status === 'succeeded') {
      log(`任务完成：${job.title}`, 'info');
      setProgress(100);
      if (job.kind === 'clone') {
        $('#side-voice').textContent = '已克隆';
        $('#voice-status').textContent = '声音克隆完成';
      }
      if (job.kind === 'complete') {
        const reports = (job.result && job.result.reports) || [];
        let done = 0, failed = 0, unconfirmed = 0;
        reports.forEach((r) => {
          done += r.done || 0; failed += r.failed || 0; unconfirmed += r.unconfirmed || 0;
        });
        log(`作业完成：成功 ${done} 题，失败 ${failed} 题`, failed ? 'warn' : 'info');
        if (unconfirmed) {
          log(`另有 ${unconfirmed} 题已提交，但 ETS 评分服务没有确认，请在官方 App 里查看`, 'warn');
        }
      }
    } else if (job.status === 'failed') {
      toast(`任务失败：${job.error || '未知错误'}`);
    } else if (job.status === 'cancelled') {
      log('任务已取消', 'warn');
    }
  }
}

/* ------------------------------------------------ 复述 / 表达：选示例答案 */
let answerPickerState = { key: '', options: [], index: 0 };

function showAnswerPicker(choose) {
  const options = Array.isArray(choose.options) ? choose.options : [];
  if (!options.length) return;
  answerPickerState = { key: choose.key || '', options, index: 0 };
  $('#answer-picker-label').textContent = choose.label || '';
  const list = $('#answer-list');
  list.innerHTML = '';
  options.forEach((text, index) => {
    const item = document.createElement('label');
    item.className = 'answer-item';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'answer-choice';
    radio.value = String(index);
    radio.checked = index === 0;
    radio.addEventListener('change', () => { answerPickerState.index = index; });
    const body = document.createElement('div');
    body.className = 'answer-body';
    const head = document.createElement('div');
    head.className = 'answer-head';
    head.textContent = `示例答案 ${index + 1} / ${options.length}`;
    const content = document.createElement('p');
    content.className = 'answer-text';
    content.textContent = text;
    body.appendChild(head);
    body.appendChild(content);
    item.appendChild(radio);
    item.appendChild(body);
    list.appendChild(item);
  });
  $('#answer-picker').classList.remove('hidden');
}

function closeAnswerPicker() {
  $('#answer-picker').classList.add('hidden');
}

$('#answer-confirm').addEventListener('click', async () => {
  const { key, index } = answerPickerState;
  closeAnswerPicker();
  const result = await api('choose_answer', key, index);
  if (!result.ok) toast(result.message || '选择未生效');
});

$('#answer-cancel').addEventListener('click', async () => {
  const { key } = answerPickerState;
  closeAnswerPicker();
  await api('choose_answer', key, 0);
});

$('#log-toggle').addEventListener('click', () => {
  const body = $('#log-body');
  body.classList.toggle('hidden');
  $('#log-toggle').textContent = body.classList.contains('hidden') ? '展开日志' : '收起日志';
});

$('#refresh-all').addEventListener('click', () => {
  if (state.tab === 'homework') loadHomework();
});

/* ------------------------------------------------------------------ boot */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function boot() {
  const result = await api('boot');
  if (!result.ok) return;
  fillSettings(result.settings);
  $('#login-phone').value = result.phone || '';
  if (result.logged_in) {
    setStatus('已登录');
    $('#side-account').textContent = `${result.grade || ''}${result.account || ''}`.trim() || '已登录';
  }
  if (result.disclaimer_required) {
    showDisclaimer(result.disclaimer_text, result.disclaimer_title);
  }
  await loadSamples();
  await loadDeviceCode();
  setInterval(pollEvents, 700);
  log('FastETS 已就绪');
}

window.addEventListener('pywebviewready', () => {
  state.ready = true;
  boot();
});

// Fallback for a plain browser (no pywebview) so the page still renders.
if (!window.pywebview) {
  window.addEventListener('load', () => {
    setTimeout(() => {
      if (!state.ready) {
        log('未检测到 pywebview 运行环境，请通过桌面应用启动。', 'warn');
      }
    }, 400);
  });
}
