// 조종 화면 동작 — 담당: 이보연
// 관제가 보내는 ui/state를 그대로 그린다 (화면 상태를 따로 저장하지 않음, 인터페이스 정의 6장).
// 버튼은 ui/state의 cars.n.allowed에 있을 때만 누를 수 있다 (설계 12장). 스페이스바 = 전체 정지 (SR-35).
// 브라우저가 기억하는 것은 사람이 고르는 중인 값(작업 목록 선택, 반복, 위치 지정 정거장, 이름 입력)과 알림 목록뿐이다.
"use strict";

const CAR_IDS = ["1", "2"];
const MODE_NAMES = { idle: "대기", manual: "조종", teach: "가르치기", auto: "혼자 하기", stopped: "정지" };
const PI_NAMES = { idle: "켜짐 (위치 모름)", driving: "이동 중", arrived: "도착", stopped: "멈춤", error: "오류" };
const MARKER_NAMES = { occupied: "● 있음", empty: "○ 없음", unknown: "◌ 보류" };
const BENCH_NAMES = { empty: "비어 있음", filling: "담는 중", filled: "담기 끝" };
const ALERT_MAX = 50;
const RECONNECT_MS = 1000;
const VIDEO_RETRY_MS = 3000;

let ws = null;
let connected = false;
let state = null;          // 마지막 ui/state
let seq = 0;
const cars = {};           // 작업차 칸 요소들

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ 보내기 (ui/cmd)

function send(action, fields = {}) {
  if (!connected) return false;
  ws.send(JSON.stringify({ topic: "ui/cmd", seq: seq++, t: Date.now() / 1000, action, ...fields }));
  return true;
}

function stopAll() {
  const sent = send("stop_all");
  const flash = $("space-flash");
  flash.textContent = sent ? "전체 정지 보냄" : "관제 연결 끊김 — 전체 정지를 보내지 못함";
  flash.hidden = false;
  clearTimeout(stopAll.timer);
  stopAll.timer = setTimeout(() => { flash.hidden = true; }, 1500);
}

// 스페이스바: 다른 입력보다 먼저 (입력 칸에 글을 쓰는 중에도) 전체 정지
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" || e.key === " ") {
    e.preventDefault();
    e.stopPropagation();
    if (!e.repeat) stopAll();
  }
}, true);
// 스페이스바를 뗄 때 포커스된 버튼이 눌리지 않게 막음
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" || e.key === " ") { e.preventDefault(); e.stopPropagation(); }
}, true);

$("btn-stop-all").addEventListener("click", stopAll);
$("btn-calib-open").addEventListener("click", () => send("hand_calib", { step: "open" }));
$("btn-calib-fist").addEventListener("click", () => send("hand_calib", { step: "fist" }));

// ------------------------------------------------------------------ 영상

// MJPEG 영상은 <img>에 주소를 한 번만 넣는다. 끊기면 "영상 없음"을 보이고 잠시 뒤 다시 연결.
function makeVideo(img, empty) {
  const v = { img, empty, url: null, on: false, timer: null, offText: "영상 없음" };
  img.addEventListener("load", () => { empty.hidden = true; img.classList.remove("broken"); });
  img.addEventListener("error", () => {
    img.classList.add("broken");
    empty.hidden = false;
    empty.textContent = "영상 없음";
    clearTimeout(v.timer);
    if (v.on) v.timer = setTimeout(() => { if (v.on) img.src = v.url + (v.url.includes("?") ? "&" : "?") + "r=" + Date.now(); }, VIDEO_RETRY_MS);
  });
  return v;
}

function setVideo(v, url, on, offText) {
  if (!on) {
    if (v.on || v.offText !== offText) {
      v.on = false; v.url = null; v.offText = offText;
      clearTimeout(v.timer);
      v.img.removeAttribute("src");
      v.empty.textContent = offText;
      v.empty.hidden = false;
    }
    return;
  }
  if (v.on && v.url === url) return;
  v.on = true; v.url = url;
  v.empty.textContent = "영상 없음";
  v.empty.hidden = false;
  v.img.src = url;
}

const mapVideo = makeVideo($("map-img"), $("map-empty"));
const handVideo = makeVideo($("hand-img"), $("hand-empty"));

// ------------------------------------------------------------------ 작업차 칸 만들기

function buildCar(n) {
  const node = $("car-template").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const c = {
    n: Number(n), node,
    name: q(".car-name"), link: q(".link"), selectChip: q(".select-chip"),
    video: makeVideo(q(".car-img"), q(".car-video .video-empty")),
    badge: q(".mode-badge"), where: q(".where"), pi: q(".pi"), progress: q(".progress"),
    stations: q(".stations"), setSelect: q(".set-station-select"),
    taskSelect: q(".task-select"), repeat: q(".repeat-box"),
    recordBox: q(".record-box"), recordName: q(".record-name"),
    deleteBox: q(".delete-box"), deleteText: q(".delete-text"),
    resultRow: q(".result-row"), resultCycle: q(".result-cycle"),
    buttons: {}, stationKey: "", taskKey: "",
  };
  c.name.textContent = `작업차 ${n}`;
  node.querySelectorAll("[data-act]").forEach((b) => { c.buttons[b.dataset.act] = b; });

  const car = c.n;
  const on = (act, fn) => c.buttons[act].addEventListener("click", fn);
  on("select", () => send("select_car", { car }));
  ["idle", "manual", "teach"].forEach((mode) => on(`set_mode:${mode}`, () => send("set_mode", { car, mode })));
  on("stop", () => send("stop", { car }));
  on("record_end", () => { c.recordBox.hidden = false; c.recordName.value = ""; c.recordName.focus(); render(); });
  on("record_cancel", () => { c.recordBox.hidden = true; render(); });
  on("record_save", () => saveRecord(c));
  c.recordName.addEventListener("input", () => render());
  c.recordName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveRecord(c);
    if (e.key === "Escape") { c.recordBox.hidden = true; render(); }
  });
  on("set_station", () => { if (c.setSelect.value) send("set_station", { car, station: c.setSelect.value }); });
  on("start", () => {
    if (c.taskSelect.value) send("start", { car, task: c.taskSelect.value, repeat: c.repeat.checked });
  });
  on("delete_ask", () => {
    const opt = c.taskSelect.selectedOptions[0];
    if (!opt || !opt.value) return;
    c.deleteBox.dataset.task = opt.value;
    c.deleteText.textContent = `"${opt.textContent}"을(를) 지울까요?`;
    c.deleteBox.hidden = false;
    render();
  });
  on("delete_no", () => { c.deleteBox.hidden = true; render(); });
  on("delete_yes", () => {
    send("delete_task", { task: c.deleteBox.dataset.task });
    c.deleteBox.hidden = true;
    render();
  });
  on("success", () => markResult(c, "success"));
  on("fail", () => markResult(c, "fail"));
  c.taskSelect.addEventListener("change", () => render());

  $("cars").appendChild(node);
  return c;
}

function saveRecord(c) {
  const name = c.recordName.value.trim();
  if (!name) return;
  if (send("record_end", { car: c.n, name })) c.recordBox.hidden = true;
  render();
}

function markResult(c, result) {
  const cs = state && state.cars[String(c.n)];
  if (cs && cs.result_cycle != null) send("mark_result", { car: c.n, cycle: cs.result_cycle, result });
}

// ------------------------------------------------------------------ 그리기

function stationName(cs, id) {
  if (id == null) return null;
  const s = cs.stations.find((x) => x.id === id);
  return s ? s.name : id;
}

function taskLabel(t) {
  // created: "2026-11-25T14:03:11" → "11-25 14:03"
  const m = /^\d{4}-(\d\d-\d\d)T(\d\d:\d\d)/.exec(t.created || "");
  return m ? `${t.name} · ${m[1]} ${m[2]}` : t.name;
}

function setMode(el, mode) {
  for (const k of Object.keys(MODE_NAMES)) el.classList.toggle(`mode-${k}`, k === mode);
}

function renderCar(c, cs, selected) {
  const allowed = connected ? cs.allowed : [];
  const can = (key) => allowed.includes(key);

  setMode(c.node, cs.mode);
  c.node.classList.toggle("selected", selected);
  c.badge.textContent = MODE_NAMES[cs.mode] || cs.mode;
  c.link.textContent = cs.link ? "● 연결" : "● 연결 끊김";
  c.link.className = "link dot-text " + (cs.link ? "good" : "bad");
  setVideo(c.video, cs.video_url, true);

  const here = stationName(cs, cs.station);
  const going = stationName(cs, cs.moving_to);
  c.where.textContent = going ? `${here || "—"} → ${going}` : (here || "—");
  c.pi.textContent = cs.pi_state ? `${PI_NAMES[cs.pi_state] || cs.pi_state}` : "—";
  c.pi.title = cs.pi_state || "";
  let prog = "—";
  if (cs.mode === "auto" && cs.task) {
    prog = `${cs.task} · ${cs.step || "—"}${/^\d+\/\d+$/.test(cs.step || "") ? " 단계" : ""}${cs.cycle ? ` · ${cs.cycle}회째` : ""}${cs.repeat ? " (반복)" : ""}`;
  } else if (cs.recording_steps != null) {
    prog = `기록 중 · ${cs.recording_steps}단계`;
  }
  c.progress.textContent = prog;

  // 버튼 (allowed에 없으면 흐리게)
  c.buttons.select.disabled = !can("select_car");
  for (const mode of ["idle", "manual", "teach"]) c.buttons[`set_mode:${mode}`].disabled = !can(`set_mode:${mode}`);
  c.buttons.record_end.disabled = !can("record_end") || !c.recordBox.hidden;
  c.buttons.stop.disabled = !can("stop");
  if (!can("record_end")) c.recordBox.hidden = true;
  c.buttons.record_save.disabled = !can("record_end") || !c.recordName.value.trim();

  // 정거장 버튼과 [위치 지정] 목록: 목록이 바뀔 때만 다시 만듦
  const key = JSON.stringify(cs.stations);
  if (key !== c.stationKey) {
    c.stationKey = key;
    c.stations.replaceChildren();
    const keep = c.setSelect.value;
    c.setSelect.replaceChildren();
    for (const s of cs.stations) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn"; b.textContent = s.name; b.dataset.station = s.id;
      b.addEventListener("click", () => send("goto", { car: c.n, station: s.id }));
      c.stations.appendChild(b);
      c.setSelect.appendChild(new Option(s.name, s.id));
    }
    if (cs.stations.some((s) => s.id === keep)) c.setSelect.value = keep;
  }
  for (const b of c.stations.children) {
    b.disabled = !can("goto");
    b.classList.toggle("here", b.dataset.station === cs.station && !cs.moving_to);
    b.classList.toggle("going", b.dataset.station === cs.moving_to);
  }
  c.setSelect.disabled = !can("set_station");
  c.buttons.set_station.disabled = !can("set_station");

  // 작업 목록: 이 작업차의 작업만
  const tasks = (state.tasks || []).filter((t) => t.car === c.n);
  const tkey = JSON.stringify(tasks);
  if (tkey !== c.taskKey) {
    c.taskKey = tkey;
    const keep = c.taskSelect.value;
    c.taskSelect.replaceChildren();
    if (!tasks.length) c.taskSelect.appendChild(new Option("저장한 작업 없음", ""));
    for (const t of tasks) c.taskSelect.appendChild(new Option(taskLabel(t), t.id));
    if (tasks.some((t) => t.id === keep)) c.taskSelect.value = keep;
  }
  const picked = c.taskSelect.value;
  c.taskSelect.disabled = !connected || !tasks.length;
  c.buttons.start.disabled = !can("start") || !picked;
  c.buttons.delete_ask.disabled = !can("delete_task") || !picked || !c.deleteBox.hidden;
  if (!can("delete_task") || !tasks.some((t) => t.id === c.deleteBox.dataset.task)) c.deleteBox.hidden = true;
  c.buttons.delete_yes.disabled = !can("delete_task");

  // 결과: 반복 한 번이 끝날 때마다 (SR-40)
  const pending = cs.result_pending && cs.result_cycle != null;
  c.resultRow.classList.toggle("pending", pending && can("mark_result"));
  c.resultCycle.textContent = pending ? `${cs.result_cycle}회째 결과를 눌러 주세요` : "";
  c.buttons.success.disabled = !can("mark_result") || !pending;
  c.buttons.fail.disabled = !can("mark_result") || !pending;
}

function render() {
  $("conn-banner").hidden = connected;
  $("btn-stop-all").disabled = !connected;
  if (!state) return;

  // 맨 위 줄: 손 인식 상태 (설계 12장)
  const h = state.hand;
  const topHand = $("top-hand");
  if (!h.service) { topHand.textContent = "● 손 인식 서비스 꺼짐"; topHand.className = "dot-text bad"; }
  else if (!h.detected) { topHand.textContent = "● 인식 끊김"; topHand.className = "dot-text bad"; }
  else { topHand.textContent = "● 인식 중"; topHand.className = "dot-text good"; }
  const topVision = $("top-vision");
  topVision.textContent = state.vision.service ? "● 연결" : "● 맵 비전 서비스 꺼짐";
  topVision.className = "dot-text " + (state.vision.service ? "good" : "bad");

  for (const n of CAR_IDS) renderCar(cars[n], state.cars[n], state.selected_car === Number(n));

  // 맵 위 영상, 보관함, 작업대
  setVideo(mapVideo, state.map_video_url, state.vision.service, "맵 비전 서비스 꺼짐");
  for (const li of $("slots").children) {
    const v = state.slots[li.dataset.slot];
    const mk = li.querySelector(".marker");
    mk.textContent = MARKER_NAMES[v] || v;
    mk.className = "marker " + v;
  }
  $("bench").textContent = BENCH_NAMES[state.bench] || state.bench;
  $("bench-vial").textContent = MARKER_NAMES[state.bench_vial] || state.bench_vial;
  $("bench-vial").className = "marker " + state.bench_vial;
  $("cock-led").textContent = state.cock_led == null ? "알 수 없음" : (state.cock_led ? "켜짐" : "꺼짐");

  // 손 인식 칸: 손이 보이면 초록 테두리, 안 보이면 빨강 + "손이 보이지 않음" (영상 위에 겹치지 않고 아래에)
  setVideo(handVideo, state.hand_video_url, h.service, "손 인식 서비스 꺼짐");
  const hv = $("hand-video");
  hv.classList.toggle("seen", h.service && h.detected);
  hv.classList.toggle("lost", h.service && !h.detected);
  $("hand-note").textContent = h.service && !h.detected ? "손이 보이지 않음" : "";
  const hs = $("hand-state");
  if (!h.service) { hs.textContent = "● 서비스 꺼짐"; hs.className = "dot-text bad"; }
  else if (!h.detected) { hs.textContent = "● 인식 끊김"; hs.className = "dot-text bad"; }
  else { hs.textContent = "● 인식 중"; hs.className = "dot-text good"; }
  $("hand-calib").textContent = h.calibrated ? "맞춤 완료" : "맞춤 전 (손 펴기 → 주먹 순서로 기록)";
  const calibOk = connected && state.cars["1"].allowed.includes("hand_calib");
  $("btn-calib-open").disabled = !calibOk;
  $("btn-calib-fist").disabled = !calibOk;
}

// ------------------------------------------------------------------ 알림

function two(n) { return String(n).padStart(2, "0"); }

function addAlert(msg) {
  const list = $("alerts");
  const empty = list.querySelector(".empty-note");
  if (empty) empty.remove();
  const d = new Date((msg.t || Date.now() / 1000) * 1000);
  const li = document.createElement("li");
  li.className = msg.level;
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
  const text = document.createElement("span");
  text.textContent = msg.text;
  text.title = msg.code;
  li.append(time, text);
  list.prepend(li);
  while (list.children.length > ALERT_MAX) list.lastElementChild.remove();
}

function showNoAlerts() {
  const li = document.createElement("li");
  li.className = "empty-note";
  li.textContent = "아직 알림 없음";
  $("alerts").appendChild(li);
}

// ------------------------------------------------------------------ 연결

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.addEventListener("open", () => { connected = true; render(); });
  ws.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.topic === "ui/state") { state = msg; render(); }
    else if (msg.topic === "ui/alert") addAlert(msg);
  });
  ws.addEventListener("close", () => {
    connected = false;
    render();
    setTimeout(connect, RECONNECT_MS);
  });
}

for (const n of CAR_IDS) cars[n] = buildCar(n);
showNoAlerts();
render();
connect();
