'use strict';
const $ = (selector) => document.querySelector(selector);
const MAX_BYTES = 25_000_000;
const extensions = new Set(['mp3','wav','m4a','mp4','webm','ogg','flac','mpeg','mpga']);
let mode = 'upload', busy = false, selectedFile = null, currentTaskId = null;
let recorder = null, displayStream = null, micStream = null, audioContext = null;
let meterFrame = null, timerId = null, meetingAnalyser = null, micAnalyser = null;
let recordingUrl = null, currentTranscript = '', pendingTaskId = null;
let currentHealth = null;

function setStatus(kind, title, text) {
  $('#statusBox').className = 'status-card ' + (kind || '');
  $('#statusTitle').textContent = title;
  $('#statusText').textContent = text;
}
function setStep(step) {
  for (let i = 1; i <= 3; i++) $('#step' + i).className = i === step ? 'current' : i < step ? 'complete' : '';
}
function setBusy(value) {
  busy = value;
  ['startBtn','chooseFileBtn','fileInput','processTextBtn','sampleBtn','meetingTitle','meetingDate','audioLanguage','outputLanguage','micToggle'].forEach((id) => $('#' + id).disabled = value);
  document.querySelectorAll('[role=tab]').forEach((tab) => tab.disabled = value);
  $('#processFileBtn').disabled = value || !selectedFile;
  $('#downloadBtn').disabled = value;
}
function switchMode(next) {
  if (busy) return;
  mode = next;
  document.querySelectorAll('[role=tab]').forEach((tab) => {
    const active = tab.dataset.source === next;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $('#' + tab.getAttribute('aria-controls')).hidden = !active;
  });
}
function metadata() {
  const title = $('#meetingTitle').value.trim();
  if (!title) { $('#meetingTitle').focus(); throw new Error('Введите название встречи.'); }
  if (!$('#meetingDate').value || !$('#meetingDate').checkValidity()) {
    $('#meetingDate').focus(); throw new Error('Укажите корректную дату встречи.');
  }
  return { meeting_title: title, meeting_date: $('#meetingDate').value, output_language: $('#outputLanguage').value };
}
function errorMessage(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || 'Проверьте поля формы.').join(' ');
  return 'Не удалось выполнить запрос. Попробуйте ещё раз.';
}
async function request(url, options = {}, timeout = 20000, blob = false) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal, cache: 'no-store' });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(errorMessage(error.detail) + (response.status === 503 ? ' Попробуйте позже.' : ''));
    }
    return blob ? await response.blob() : await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Сервер не ответил вовремя. Проверьте, запущено ли приложение.');
    if (error instanceof TypeError) throw new Error('Нет связи с сервером. Запустите start.bat и повторите попытку.');
    throw error;
  } finally { clearTimeout(timer); }
}
async function refreshSetupStatus() {
  $('#refreshSetupBtn').disabled = true;
  try {
    const health = await request('/health', {}, 8000);
    currentHealth = health;
    $('#connectionLabel').className = 'connection online';
    $('#connectionLabel').lastChild.textContent = 'Сервер на компьютере';
    $('#whisperName').textContent = 'Whisper · ' + health.whisper_model;
    $('#ollamaName').textContent = health.ollama_model;
    $('#whisperDot').className = 'model-dot ' + (health.transcription_ready ? 'ready' : 'missing');
    $('#ollamaDot').className = 'model-dot ' + (health.summarization_ready ? 'ready' : 'missing');
    const missing = [];
    if (!health.transcription_ready) missing.push('Для аудио: перезапустите start.bat, чтобы установить Faster-Whisper.');
    if (!health.ollama_online) missing.push('Для автоматического анализа запустите setup_models.bat.');
    else if (!health.ollama_model_available) missing.push('Загрузите модель: ollama pull ' + health.ollama_model + '.');
    $('#setupStatus').className = 'setup ' + (missing.length ? '' : 'ready');
    $('#setupStatus').textContent = missing.length ? missing.join(' ') + ' Редактор текста уже доступен.' : 'Всё готово. Аудио и текст обрабатываются локально.';
  } catch (error) {
    currentHealth = null;
    $('#connectionLabel').className = 'connection';
    $('#connectionLabel').lastChild.textContent = 'Сервер недоступен';
    $('#setupStatus').className = 'setup';
    $('#setupStatus').textContent = error.message;
    $('#whisperDot').className = $('#ollamaDot').className = 'model-dot missing';
  } finally { $('#refreshSetupBtn').disabled = false; }
}
function selectFile(file) {
  if (busy || !file) return;
  const extension = file.name.split('.').pop().toLowerCase();
  if (!extensions.has(extension)) { setStatus('error','Неподдерживаемый формат','Выберите MP3, WAV, M4A, MP4, WebM, OGG или FLAC.'); return; }
  if (!file.size || file.size > MAX_BYTES) { setStatus('error','Проверьте размер файла', file.size ? 'Максимальный размер — 25 МБ. Разделите запись на части.' : 'Файл пустой. Выберите другую запись.'); return; }
  selectedFile = file;
  $('#fileTitle').textContent = file.name;
  $('#fileDescription').textContent = (file.size / 1_000_000).toFixed(2) + ' МБ · готов к обработке';
  $('#chooseFileBtn').firstChild.textContent = 'Заменить файл ';
  $('#processFileBtn').disabled = false;
  setStatus('', 'Запись добавлена', 'Проверьте название и язык, затем нажмите «Создать протокол».');
}
async function startProcessing(kind, blob, filename) {
  if (busy) return;
  let info;
  try {
    info = metadata();
    if (kind === 'text' && !$('#transcriptInput').value.trim()) throw new Error('Вставьте текст встречи или воспользуйтесь учебным примером.');
    if (kind === 'audio' && (!blob || !blob.size || blob.size > MAX_BYTES)) throw new Error('Нужна непустая запись размером до 25 МБ.');
    if (kind === 'audio' && currentHealth && !currentHealth.transcription_ready) throw new Error('Для распознавания установите Faster-Whisper: перезапустите start.bat.');
  } catch (error) { setStatus('error','Проверьте данные',error.message); return; }
  setBusy(true);
  $('#resumeBtn').hidden = true;
  setStep(2);
  setStatus('processing','Передаём на локальный сервер','Данные остаются на вашем компьютере. Это может занять несколько минут.');
  try {
    let response;
    if (kind === 'text') {
      response = await request('/transcript', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...info, transcript_text:$('#transcriptInput').value.trim()})});
    } else {
      const form = new FormData();
      form.append('file',blob,filename);
      Object.entries(info).forEach(([key,value]) => form.append(key,value));
      form.append('language',$('#audioLanguage').value);
      response = await request('/transcribe',{method:'POST',body:form},60000);
    }
    pendingTaskId = response.task_id;
    try { sessionStorage.setItem('meeting-notes-pending',pendingTaskId); } catch {}
    await pollTask(pendingTaskId);
  } catch (error) {
    setStatus('error','Не удалось завершить обработку',error.message);
    $('#resumeBtn').hidden = !pendingTaskId;
  } finally { setBusy(false); }
}
function forgetPending() {
  pendingTaskId = null;
  try { sessionStorage.removeItem('meeting-notes-pending'); } catch {}
  $('#resumeBtn').hidden = true;
}
async function pollTask(taskId) {
  const deadline = Date.now() + 60 * 60 * 1000;
  let failures = 0;
  while (Date.now() < deadline) {
    let task;
    try { task = await request('/transcribe/' + encodeURIComponent(taskId)); failures = 0; }
    catch (error) {
      if (++failures >= 3) throw error;
      setStatus('processing','Восстанавливаем соединение','Обработка продолжится на сервере. Повторяем проверку…');
      await new Promise((resolve) => setTimeout(resolve,2000));
      continue;
    }
    if (task.status === 'error') { forgetPending(); throw new Error(task.error || 'Ошибка обработки.'); }
    if (task.status === 'done') {
      currentTaskId = taskId;
      forgetPending();
      showResult(task.result);
      return;
    }
    const descriptions = {
      queued: ['Встреча в очереди','Дождитесь завершения предыдущей записи.'],
      transcription: ['Распознаём речь','Первый запуск включает загрузку модели. Можно оставить эту вкладку открытой.'],
      analysis: ['Собираем протокол','Выделяем решения и задачи, проверяем цитаты из встречи.']
    };
    const [title, text] = descriptions[task.stage] || ['Обрабатываем встречу','Подождите, пожалуйста.'];
    setStatus('processing',title,text);
    await new Promise((resolve) => setTimeout(resolve,1500));
  }
  throw new Error('Обработка ещё не завершена. Нажмите «Продолжить ожидание», чтобы проверить результат.');
}
async function resumeProcessing() {
  if (!pendingTaskId || busy) return;
  setBusy(true); setStep(2); $('#resumeBtn').hidden = true;
  try { await pollTask(pendingTaskId); }
  catch (error) { setStatus('error','Ожидание прервано',error.message); $('#resumeBtn').hidden = !pendingTaskId; }
  finally { setBusy(false); }
}
function stopTracks() {
  displayStream?.getTracks().forEach((track) => track.stop());
  micStream?.getTracks().forEach((track) => track.stop());
  if (audioContext && audioContext.state !== 'closed') audioContext.close().catch(() => {});
  displayStream = micStream = audioContext = null;
  meetingAnalyser = micAnalyser = null;
  clearInterval(timerId);
  if (meterFrame !== null) cancelAnimationFrame(meterFrame);
  meterFrame = null;
  $('#meetingLevel').value = $('#micLevel').value = 0;
  $('#meetingLevelLabel').textContent = $('#micLevelLabel').textContent = 'Ожидание';
}
function updateMeters() {
  [[meetingAnalyser,'meeting'],[micAnalyser,'mic']].forEach(([analyser,id]) => {
    let level = 0;
    if (analyser) {
      const samples = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(samples);
      level = Math.min(100, Math.round(Math.sqrt(samples.reduce((sum,value) => sum + ((value-128)/128)**2,0)/samples.length)*320));
    }
    $('#' + id + 'Level').value = level;
    $('#' + id + 'LevelLabel').textContent = !analyser ? 'Выключен' : level > 2 ? 'Есть звук' : 'Тишина';
  });
  if (recorder?.state === 'recording') meterFrame = requestAnimationFrame(updateMeters);
}
async function startRecording() {
  if (busy) return;
  try {
    metadata();
    if (!navigator.mediaDevices?.getDisplayMedia || !window.MediaRecorder) throw new Error('Для записи откройте приложение в актуальном Chrome или Edge по адресу http://127.0.0.1:8000. Загрузка файлов доступна в любом браузере.');
    setBusy(true);
    // Keep display capture inside the click gesture; do not await a health request.
    displayStream = await navigator.mediaDevices.getDisplayMedia({video:true,audio:true});
    if (!displayStream.getAudioTracks().length) throw new Error('Не выбран звук встречи. Выберите вкладку и включите «Передавать звук вкладки» либо системный звук.');
    audioContext = new AudioContext();
    await audioContext.resume();
    const destination = audioContext.createMediaStreamDestination();
    const source = audioContext.createMediaStreamSource(new MediaStream(displayStream.getAudioTracks()));
    source.connect(destination);
    meetingAnalyser = audioContext.createAnalyser(); meetingAnalyser.fftSize = 512;
    source.connect(meetingAnalyser);
    if ($('#micToggle').checked) {
      micStream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      const mic = audioContext.createMediaStreamSource(micStream);
      mic.connect(destination);
      micAnalyser = audioContext.createAnalyser(); micAnalyser.fftSize = 512;
      mic.connect(micAnalyser);
    }
    const mimeType = ['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/mp4'].find((type) => MediaRecorder.isTypeSupported(type));
    recorder = new MediaRecorder(destination.stream,{...(mimeType ? {mimeType} : {}),audioBitsPerSecond:64000});
    const activeRecorder = recorder, chunks = [];
    let bytes = 0, failed = false;
    activeRecorder.ondataavailable = (event) => {
      if (!event.data.size) return;
      chunks.push(event.data); bytes += event.data.size;
      if (bytes >= 24_000_000 && activeRecorder.state === 'recording') stopRecording();
    };
    activeRecorder.onerror = () => { failed = true; stopTracks(); setBusy(false); $('#stopBtn').disabled = true; setStatus('error','Запись прервана','Браузер не смог продолжить запись. Сохраните доступное аудио и повторите.'); };
    activeRecorder.onstop = async () => {
      stopTracks(); $('#stopBtn').disabled = true;
      const type = activeRecorder.mimeType || mimeType || 'audio/webm';
      const extension = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
      const blob = new Blob(chunks,{type});
      recorder = null;
      if (recordingUrl) URL.revokeObjectURL(recordingUrl);
      recordingUrl = URL.createObjectURL(blob);
      $('#saveRecording').href = recordingUrl;
      $('#saveRecording').download = 'meeting-' + $('#meetingDate').value + '.' + extension;
      $('#saveRecording').hidden = !blob.size;
      setBusy(false);
      if (!failed) await startProcessing('audio',blob,'meeting.' + extension);
    };
    displayStream.getVideoTracks()[0]?.addEventListener('ended',stopRecording,{once:true});
    activeRecorder.start(1000);
    const started = Date.now();
    $('#recordTimer').textContent = '00:00';
    timerId = setInterval(() => {
      const seconds = Math.floor((Date.now()-started)/1000);
      $('#recordTimer').textContent = String(Math.floor(seconds/60)).padStart(2,'0') + ':' + String(seconds%60).padStart(2,'0');
    },1000);
    updateMeters(); $('#stopBtn').disabled = false;
    setStatus('recording','Идёт запись встречи','Проверьте шкалу звука. После остановки начнётся обработка; исходную запись можно сохранить.');
  } catch (error) {
    stopTracks(); setBusy(false); $('#stopBtn').disabled = true;
    setStatus('error','Запись не началась',error.name === 'NotAllowedError' ? 'Доступ к звуку не предоставлен или выбор источника отменён. Можно повторить или загрузить готовый файл.' : error.message);
  }
}
function stopRecording() {
  if (recorder?.state === 'recording') {
    $('#stopBtn').disabled = true;
    setStatus('processing','Сохраняем запись','Готовим аудио для обработки.');
    recorder.stop();
  }
}
function makeDecisionRow(value = '') {
  if ($('#decisionList').children.length >= 100) { setStatus('error','Достигнут лимит','В одном протоколе может быть до 100 решений.'); return; }
  const row = document.createElement('div'); row.className = 'decision-row';
  const input = document.createElement('input'); input.className = 'decision-input';
  input.value = value; input.maxLength = 2000; input.placeholder = 'Решение, принятое на встрече'; input.setAttribute('aria-label','Принятое решение');
  const remove = document.createElement('button'); remove.className = 'icon-button'; remove.textContent = '×'; remove.setAttribute('aria-label','Удалить решение');
  remove.addEventListener('click',() => row.remove()); row.append(input,remove); $('#decisionList').append(row);
}
function makeTaskRow(item = {}) {
  if ($('#taskRows').children.length >= 200) { setStatus('error','Достигнут лимит','В одном протоколе может быть до 200 задач.'); return; }
  const row = document.createElement('tr');
  const number = document.createElement('td'); number.className = 'task-number'; row.append(number);
  const labels = {task:'Что сделать',responsible:'Ответственный',deadline:'Срок',evidence:'Цитата-основание'};
  Object.entries(labels).forEach(([key,label]) => {
    const cell = document.createElement('td'), input = document.createElement(key === 'evidence' || key === 'task' ? 'textarea' : 'input');
    input.className = 'task-input'; input.dataset.field = key; input.setAttribute('aria-label',label);
    input.value = item[key] || (key === 'evidence' && item.manual ? 'Добавлено пользователем' : '');
    input.maxLength = ['task','evidence'].includes(key) ? 2000 : 300;
    input.placeholder = key === 'task' ? 'Что нужно сделать' : key === 'evidence' ? 'Цитата из встречи' : 'Не определено';
    if (key === 'evidence') input.readOnly = !item.manual;
    cell.append(input); row.append(cell);
  });
  const cell = document.createElement('td'), remove = document.createElement('button');
  remove.className = 'icon-button'; remove.textContent = '×'; remove.setAttribute('aria-label','Удалить задачу');
  remove.addEventListener('click',() => { row.remove(); renumberTasks(); }); cell.append(remove); row.append(cell);
  $('#taskRows').append(row); renumberTasks();
}
function renumberTasks() {
  const rows = [...$('#taskRows').children];
  rows.forEach((row,index) => row.cells[0].textContent = String(index+1));
  $('#taskCount').textContent = String(rows.length);
  $('#emptyTasks').hidden = !!rows.length;
  $('#tasksTable').hidden = !rows.length;
}
function showResult(result) {
  $('#summaryText').value = result.summary || '';
  $('#meetingTitle').value = result.meeting_title;
  $('#meetingDate').value = result.meeting_date;
  $('#outputLanguage').value = result.output_language || 'kk';
  $('#speakersInput').value = (result.speakers || []).join('\n');
  $('#decisionList').replaceChildren(); (result.decisions || []).forEach(makeDecisionRow);
  $('#taskRows').replaceChildren(); (result.tasks || []).forEach(makeTaskRow); renumberTasks();
  currentTranscript = result.transcript_text || '';
  $('#transcriptText').textContent = currentTranscript || 'Речь не распознана.';
  $('#qualityWarning').textContent = result.quality_warning || (result.status === 'unclear' ? 'Не удалось уверенно распознать речь. Проверьте исходную запись.' : '');
  $('#qualityWarning').hidden = !$('#qualityWarning').textContent;
  $('#resultBox').hidden = false;
  $('#openResultBtn').disabled = false; $('#resultIndicator').hidden = false;
  setStep(3);
  setStatus('',result.status === 'manual' ? 'Текст готов к ручной проверке' : 'Черновик готов',result.status === 'manual' ? 'Заполните резюме, решения и задачи по расшифровке, затем скачайте Word.' : 'Уточните детали и проверьте цитаты. Word сохранит ваши исправления.');
  $('#resultBox').scrollIntoView({behavior:'smooth',block:'start'});
}
async function downloadDocument() {
  if (!currentTaskId || busy) return;
  $('#downloadBtn').disabled = true;
  try {
    const summary = $('#summaryText').value.trim();
    if (!summary) { $('#summaryText').focus(); throw new Error('Заполните краткое резюме перед скачиванием.'); }
    const speakers = $('#speakersInput').value.split('\n').map((name) => name.trim()).filter(Boolean);
    if (speakers.length > 100 || speakers.some((name) => name.length > 300)) throw new Error('Укажите до 100 участников, не более 300 символов в одном имени.');
    const tasks = [...$('#taskRows').children].map((row) => Object.fromEntries([...row.querySelectorAll('[data-field]')].map((field) => [field.dataset.field,field.value.trim()]))).filter((item) => item.task);
    const payload = {...metadata(),summary,speakers,tasks,
      decisions:[...document.querySelectorAll('.decision-input')].map((input) => input.value.trim()).filter(Boolean),
      transcript_text:$('#includeTranscript').checked ? currentTranscript : null};
    const blob = await request('/transcribe/' + encodeURIComponent(currentTaskId) + '/document',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},30000,true);
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'meeting_minutes_' + payload.meeting_date.replaceAll('-','') + '.docx';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),10000);
    setStatus('','Документ Word скачан','В документ включены ваши исправления' + (payload.transcript_text ? ' и полная расшифровка.' : '.'));
  } catch (error) { setStatus('error','Не удалось скачать документ',error.message); $('#statusBox').scrollIntoView({behavior:'smooth',block:'center'}); }
  finally { $('#downloadBtn').disabled = false; }
}
document.querySelectorAll('[role=tab]').forEach((tab) => {
  tab.addEventListener('click',() => switchMode(tab.dataset.source));
  tab.addEventListener('keydown',(event) => {
    const tabs = [...document.querySelectorAll('[role=tab]')];
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (tabs.indexOf(tab)+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    switchMode(tabs[index].dataset.source); tabs[index].focus();
  });
});
$('#chooseFileBtn').addEventListener('click',() => $('#fileInput').click());
$('#fileInput').addEventListener('change',(event) => { selectFile(event.target.files[0]); event.target.value = ''; });
['dragenter','dragover'].forEach((name) => $('#dropzone').addEventListener(name,(event) => { event.preventDefault(); if (!busy) $('#dropzone').classList.add('dragging'); }));
['dragleave','drop'].forEach((name) => $('#dropzone').addEventListener(name,(event) => { event.preventDefault(); $('#dropzone').classList.remove('dragging'); if (name === 'drop') selectFile(event.dataTransfer.files[0]); }));
window.addEventListener('dragover',(event) => event.preventDefault());
window.addEventListener('drop',(event) => event.preventDefault());
$('#processFileBtn').addEventListener('click',() => startProcessing('audio',selectedFile,selectedFile?.name));
$('#processTextBtn').addEventListener('click',() => startProcessing('text'));
$('#startBtn').addEventListener('click',startRecording);
$('#stopBtn').addEventListener('click',stopRecording);
$('#refreshSetupBtn').addEventListener('click',refreshSetupStatus);
$('#resumeBtn').addEventListener('click',resumeProcessing);
$('#addDecisionBtn').addEventListener('click',() => makeDecisionRow());
$('#addTaskBtn').addEventListener('click',() => makeTaskRow({manual:true}));
$('#downloadBtn').addEventListener('click',downloadDocument);
$('#openResultBtn').addEventListener('click',() => $('#resultBox').scrollIntoView({behavior:'smooth'}));
function updateCharacterCount() { $('#characterCount').textContent = $('#transcriptInput').value.length.toLocaleString('ru-RU') + ' / 60 000'; }
$('#transcriptInput').addEventListener('input',updateCharacterCount);
$('#sampleBtn').addEventListener('click',() => {
  $('#transcriptInput').value = 'Учебный пример встречи.\nАйжан: Меня зовут Айжан. Предлагаю запустить тестовую версию в понедельник.\nДанияр: Я Данияр. Согласен. Решили запустить тестовую версию в понедельник.\nАйжан: Я подготовлю план запуска к пятнице.\nДанияр: Я проверю форму регистрации до воскресенья.\nАйжан: Договорились, итоги обсудим на следующей встрече.';
  updateCharacterCount();
});
window.addEventListener('beforeunload',(event) => { if (recorder?.state === 'recording') { event.preventDefault(); event.returnValue = ''; } });
const today = new Date();
$('#meetingDate').value = today.getFullYear() + '-' + String(today.getMonth()+1).padStart(2,'0') + '-' + String(today.getDate()).padStart(2,'0');
$('#todayLabel').textContent = new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric'}).format(today);
refreshSetupStatus();
try { pendingTaskId = sessionStorage.getItem('meeting-notes-pending'); } catch {}
if (pendingTaskId) resumeProcessing();

