// 조종 화면 동작 — 담당: 이보연
// 관제가 보내는 ui/state를 그대로 그린다 (화면 상태를 따로 저장하지 않음, 인터페이스 정의 6장).
// 배치 (설계 12장): 조종하는 작업차(selected_car)를 큰 칸에, 다른 작업차를 오른쪽 작은 칸에.
// 버튼은 ui/state의 cars.n.allowed에 있을 때만 누를 수 있다. 스페이스바 = 전체 정지 (SR-35), Enter = [놓기 허용] (설계 9.7).
// 브라우저가 기억하는 것은 사람이 고르는 중인 값(작업차별 작업 선택·반복, 위치 지정 정거장, 이름 입력)과 알림 목록뿐이다.
"use strict";

const MODE_NAMES = { idle: "대기", manual: "조종", teach: "가르치기", auto: "혼자 하기", stopped: "정지" };
const PI_NAMES = { idle: "켜짐 (위치 모름)", driving: "이동 중", arrived: "도착", stopped: "멈춤", error: "오류" };
const MARKER_NAMES = { occupied: "● 있음", empty: "○ 없음", unknown: "◌ 보류" };
const BENCH_NAMES = { empty: "비어 있음", filling: "담는 중", filled: "담기 끝" };
const FINGER_NAMES = ["엄지", "검지", "중지", "약지", "새끼"];
const ALERT_MAX = 50;
const RECONNECT_MS = 1000;
const VIDEO_RETRY_MS = 3000;

// 가: 손 상태 줄 (ui/state cars.n.hand_ctl, 인터페이스 정의 6.1 / 설계 9.6·9.7). [글자, 색]
const HAND_TEXT = {
  not_selected: ["조종 대상이 아님", "quiet"],
  mode: ["✋ [조종] 또는 [가르치기]를 누르면 손을 따라 합니다", "quiet"],
  link: ["✋ 작업차 연결이 끊겨 손을 따라 하지 않음", "bad"],
  no_calib: ["✋ 이 작업차 AiHand 보정 파일이 없어 손을 따라 하지 않음", "bad"],
  hand_service: ["✋ 손 인식 서비스가 꺼져 있음 — 손 인식 서비스를 다시 실행하세요", "bad"],
  hand_uncalibrated: ["✋ 손 맞춤을 먼저 하세요 — 오른쪽 [손 펴기 기록] → [주먹 기록]", "warn"],
  hand_lost: ["✋ 손이 보이지 않음 — 웹캠 앞에 손을 보여 주세요 (AiHand는 마지막 자세 유지)", "bad"],
  moving: ["✋ 이동 중 — 도착하면 손을 쫙 펴서 다시 시작", "quiet"],
  locked: ["✋ 손을 쫙 펴서 시작하세요 (편 손은 따라 하지 않음)", "warn"],
  release_hold: ["⚠ 놓는 곳이 아닙니다", "bad"],
  catching: ["✋ 손가락을 AiHand 모양에 맞추세요 — 닿은 손가락부터 따라 함", "info"],
  following: ["✋ 손을 따라 하는 중", "good"],
};

let ws = null;
let connected = false;
let state = null;          // 마지막 ui/state
let seq = 0;
const picked = {};         // 작업차별로 고른 작업 id
const repeatOn = {};       // 작업차별 [반복]
let bigCar = null;         // 지금 큰 칸에 그린 작업차 (바뀌면 입력 상자를 닫음)
let stationKey = "";
let taskKey = "";

const $ = (id) => document.getElementById(id);
const big = $("big");
const small = $("small");
const bigBtn = {};
big.querySelectorAll("[data-act]").forEach((b) => { bigBtn[b.dataset.act] = b; });
const smallBtn = {};
small.querySelectorAll("[data-sact]").forEach((b) => { smallBtn[b.dataset.sact] = b; });

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

function allowRelease() {
  if (!state) return false;
  const car = state.selected_car;
  if (!state.cars[String(car)].allowed.includes("allow_release")) return false;
  return send("allow_release", { car });
}

// 스페이스바: 다른 입력보다 먼저 (입력 칸에 글을 쓰는 중에도) 전체 정지
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" || e.key === " ") {
    e.preventDefault();
    e.stopPropagation();
    if (!e.repeat) stopAll();
  }
}, true);
// Enter: [놓기 허용] (설계 9.7). 이름 입력 칸에서 누른 Enter는 그 칸이 처리(이름 저장)
window.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.repeat) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
  if (allowRelease()) e.preventDefault();
});
// 스페이스바를 뗄 때 포커스된 버튼이 눌리지 않게 막음
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" || e.key === " ") { e.preventDefault(); e.stopPropagation(); }
}, true);

$("btn-stop-all").addEventListener("click", stopAll);
$("btn-release").addEventListener("click", allowRelease);
$("btn-calib-open").addEventListener("click", () => send("hand_calib", { step: "open" }));
$("btn-calib-fist").addEventListener("click", () => send("hand_calib", { step: "fist" }));

// 큰 칸 버튼: 언제나 조종 대상(selected_car)에 적용
const sel = () => (state ? state.selected_car : 1);
["idle", "manual", "teach"].forEach((mode) =>
  bigBtn[`set_mode:${mode}`].addEventListener("click", () => send("set_mode", { car: sel(), mode })));
bigBtn.stop.addEventListener("click", () => send("stop", { car: sel() }));
bigBtn.record_end.addEventListener("click", () => {
  $("record-box").hidden = false; $("record-name").value = ""; $("record-name").focus(); render();
});
bigBtn.record_cancel.addEventListener("click", () => { $("record-box").hidden = true; render(); });
bigBtn.record_save.addEventListener("click", saveRecord);
$("record-name").addEventListener("input", () => render());
$("record-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveRecord();
  if (e.key === "Escape") { $("record-box").hidden = true; render(); }
});
bigBtn.set_station.addEventListener("click", () => {
  const st = $("set-station-select").value;
  if (st) send("set_station", { car: sel(), station: st });
});
$("task-select").addEventListener("change", () => { picked[sel()] = $("task-select").value; render(); });
$("repeat-box").addEventListener("change", () => { repeatOn[sel()] = $("repeat-box").checked; });
bigBtn.start.addEventListener("click", () => {
  const task = $("task-select").value;
  if (task) send("start", { car: sel(), task, repeat: $("repeat-box").checked });
});
bigBtn.delete_ask.addEventListener("click", () => {
  const opt = $("task-select").selectedOptions[0];
  if (!opt || !opt.value) return;
  $("delete-box").dataset.task = opt.value;
  $("delete-text").textContent = `"${opt.textContent}"을(를) 지울까요?`;
  $("delete-box").hidden = false;
  render();
});
bigBtn.delete_no.addEventListener("click", () => { $("delete-box").hidden = true; render(); });
bigBtn.delete_yes.addEventListener("click", () => {
  send("delete_task", { task: $("delete-box").dataset.task });
  $("delete-box").hidden = true;
  render();
});
bigBtn.success.addEventListener("click", () => markResult(sel(), "success"));
bigBtn.fail.addEventListener("click", () => markResult(sel(), "fail"));

// 작은 칸 버튼: 다른 작업차에 적용
const other = () => (sel() === 1 ? 2 : 1);
smallBtn.stop.addEventListener("click", () => send("stop", { car: other() }));
smallBtn.success.addEventListener("click", () => markResult(other(), "success"));
smallBtn.fail.addEventListener("click", () => markResult(other(), "fail"));
smallBtn.select.addEventListener("click", () => send("select_car", { car: other() }));

function saveRecord() {
  const name = $("record-name").value.trim();
  if (!name) return;
  if (send("record_end", { car: sel(), name })) $("record-box").hidden = true;
  render();
}

function markResult(car, result) {
  const cs = state && state.cars[String(car)];
  if (cs && cs.result_cycle != null) send("mark_result", { car, cycle: cs.result_cycle, result });
}

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
  v.img.classList.remove("broken");
  v.img.src = url;
}

const bigVideo = makeVideo($("big-img"), $("big-empty"));
const mapVideo = makeVideo($("map-img"), $("map-empty"));
const handVideo = makeVideo($("hand-img"), $("hand-empty"));

// ------------------------------------------------------------------ 글자 만들기

function stationName(cs, id) {
  if (id == null) return null;
  const s = cs.stations.find((x) => x.id === id);
  return s ? s.name : id;
}

function whereText(cs) {
  const here = stationName(cs, cs.station);
  const going = stationName(cs, cs.moving_to);
  return going ? `${here || "—"} → ${going}` : (here || "—");
}

function progressText(cs) {
  if (cs.mode === "auto" && cs.task) {
    const unit = /^\d+\/\d+$/.test(cs.step || "") ? " 단계" : "";
    return `${cs.task} · ${cs.step || "—"}${unit}${cs.cycle ? ` · ${cs.cycle}회째` : ""}${cs.repeat ? " (반복)" : ""}`;
  }
  if (cs.recording_steps != null) return `기록 중 · ${cs.recording_steps}단계`;
  return "—";
}

function taskLabel(t) {
  // created: "2026-11-25T14:03:11" → "11-25 14:03"
  const m = /^\d{4}-(\d\d-\d\d)T(\d\d:\d\d)/.exec(t.created || "");
  return m ? `${t.name} · ${m[1]} ${m[2]}` : t.name;
}

// 나: 다음 할 일 (이미 받는 ui/state 값으로 판단)
function nextText(cs) {
  if (!cs.link) return "다음: 작업차 연결을 기다리는 중 (관제가 1초마다 다시 접속)";
  if (cs.pi_state === "error")
    return "다음: 작업차를 손으로 가까운 정거장 위에 올리고 <b>[위치 지정]</b> → <b>[조종]</b>";
  if (cs.pi_state === "idle" || cs.station == null)
    return "다음: 작업차를 정거장 위에 올리고, 아래에서 그 정거장을 골라 <b>[위치 지정]</b>";
  if (cs.result_pending && cs.result_cycle != null)
    return `다음: 방금 끝난 ${cs.result_cycle}회째 결과를 <b>[성공]</b> / <b>[실패]</b>로 표시`;
  switch (cs.mode) {
    case "stopped": return "다음: <b>[조종]</b>을 눌러 넘겨받으세요 (정지 뒤에는 조종만 할 수 있음)";
    case "idle": return "다음: <b>[조종]</b>으로 손 조종을 하거나, 작업을 골라 <b>[시작]</b> (대기 정거장에서만)";
    case "manual": return cs.hand_ctl === "moving" ? "이동 중…" : "다음: 정거장 버튼으로 옮기거나, 손으로 AiHand를 조종";
    case "teach": return `가르치는 중 (${cs.recording_steps || 0}단계) — 다 했으면 <b>[기록 끝]</b>`;
    case "auto": return "혼자 하는 중 — 멈추려면 <b>[정지]</b> 또는 Space";
    default: return "";
  }
}

function setMode(el, mode) {
  for (const k of Object.keys(MODE_NAMES)) el.classList.toggle(`mode-${k}`, k === mode);
}

function setModeChip(el, mode) {
  setMode(el, mode);
  el.textContent = MODE_NAMES[mode] || mode;
}

function setLink(el, up) {
  el.textContent = up ? "● 연결" : "● 연결 끊김";
  el.className = "link dot-text " + (up ? "good" : "bad");
}

function bar(r, on, unknown) {
  const d = document.createElement("div");
  d.className = "bar" + (on ? " on" : "") + (unknown ? " unknown" : "");
  const fill = document.createElement("i");
  fill.style.width = unknown ? "0%" : `${Math.round((r || 0) * 100)}%`;
  d.appendChild(fill);
  return d;
}

// ------------------------------------------------------------------ 그리기

function renderHandLine(cs) {
  const ctl = cs.hand_ctl;
  const [text, tone] = HAND_TEXT[ctl] || [ctl, "quiet"];
  const line = $("hand-line");
  line.className = "hand-line " + tone;
  let t = text;
  if (ctl === "release_hold") t = `⚠ 놓는 곳이 아닙니다 (지금: ${stationName(cs, cs.station) || "—"}). 다시 쥐거나`;
  if (ctl === "catching" && cs.aihand_fingers == null) t = "✋ AiHand 모양을 모름 — 주먹을 쥐면 이어집니다";
  $("hand-line-text").textContent = t;
  const rel = $("btn-release");
  rel.hidden = ctl !== "release_hold";
  rel.disabled = !(connected && cs.allowed.includes("allow_release"));

  // 이어 붙이는 중: AiHand와 내 손 막대 (붙은 손가락은 초록)
  const bars = $("bars");
  bars.hidden = ctl !== "catching";
  if (!bars.hidden) {
    const a = $("bars-aihand"), h = $("bars-hand");
    a.replaceChildren(); h.replaceChildren();
    const mine = state.hand.fingers;
    for (let i = 0; i < 5; i++) {
      const on = cs.hand_attached[i];
      const ab = bar(cs.aihand_fingers ? cs.aihand_fingers[i] : 1, on, cs.aihand_fingers == null);
      ab.title = FINGER_NAMES[i];
      a.appendChild(ab);
      const hb = bar(mine ? mine[i] : 0, on, mine == null);
      hb.title = FINGER_NAMES[i];
      h.appendChild(hb);
    }
  }
}

function renderBig(cs, n) {
  const can = (key) => connected && cs.allowed.includes(key);
  if (bigCar !== n) {                      // 큰 칸의 작업차가 바뀜: 열린 입력 상자를 닫고 목록을 다시 만듦
    bigCar = n;
    $("record-box").hidden = true;
    $("delete-box").hidden = true;
    stationKey = taskKey = "";
  }
  setMode(big, cs.mode);
  $("big-name").textContent = `작업차 ${n}`;
  setModeChip($("big-mode"), cs.mode);
  setLink($("big-link"), cs.link);
  setVideo(bigVideo, cs.video_url, true);
  $("big-where").textContent = whereText(cs);
  $("big-pi").textContent = cs.pi_state ? (PI_NAMES[cs.pi_state] || cs.pi_state) : "—";
  $("big-pi").title = cs.pi_state || "";
  $("big-progress").textContent = progressText(cs);
  $("next").innerHTML = nextText(cs);
  renderHandLine(cs);

  for (const mode of ["idle", "manual", "teach"]) bigBtn[`set_mode:${mode}`].disabled = !can(`set_mode:${mode}`);
  bigBtn.stop.disabled = !can("stop");
  if (!can("record_end")) $("record-box").hidden = true;
  bigBtn.record_end.disabled = !can("record_end") || !$("record-box").hidden;
  bigBtn.record_save.disabled = !can("record_end") || !$("record-name").value.trim();

  // 정거장 버튼과 [위치 지정] 목록: 목록이 바뀔 때만 다시 만듦
  const key = n + JSON.stringify(cs.stations);
  if (key !== stationKey) {
    stationKey = key;
    const box = $("stations");
    const selBox = $("set-station-select");
    box.replaceChildren();
    selBox.replaceChildren();
    for (const s of cs.stations) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn"; b.textContent = s.name; b.dataset.station = s.id;
      b.addEventListener("click", () => send("goto", { car: sel(), station: s.id }));
      box.appendChild(b);
      selBox.appendChild(new Option(s.name, s.id));
    }
  }
  for (const b of $("stations").children) {
    b.disabled = !can("goto");
    b.classList.toggle("here", b.dataset.station === cs.station && !cs.moving_to);
    b.classList.toggle("going", b.dataset.station === cs.moving_to);
  }
  $("set-station-select").disabled = !can("set_station");
  bigBtn.set_station.disabled = !can("set_station");

  // 작업 목록: 이 작업차의 작업만
  const tasks = (state.tasks || []).filter((t) => t.car === n);
  const tkey = n + JSON.stringify(tasks);
  const ts = $("task-select");
  if (tkey !== taskKey) {
    taskKey = tkey;
    ts.replaceChildren();
    if (!tasks.length) ts.appendChild(new Option("저장한 작업 없음", ""));
    for (const t of tasks) ts.appendChild(new Option(taskLabel(t), t.id));
    if (tasks.some((t) => t.id === picked[n])) ts.value = picked[n];
    $("repeat-box").checked = !!repeatOn[n];
  }
  const chosen = ts.value;
  ts.disabled = !connected || !tasks.length;
  bigBtn.start.disabled = !can("start") || !chosen;
  bigBtn.delete_ask.disabled = !can("delete_task") || !chosen || !$("delete-box").hidden;
  if (!can("delete_task") || !tasks.some((t) => t.id === $("delete-box").dataset.task)) $("delete-box").hidden = true;
  bigBtn.delete_yes.disabled = !can("delete_task");

  // 결과: 반복 한 번이 끝날 때마다 (SR-40)
  const pending = cs.result_pending && cs.result_cycle != null;
  $("big-result-cycle").textContent = pending ? `${cs.result_cycle}회째` : "";
  bigBtn.success.disabled = !can("mark_result") || !pending;
  bigBtn.fail.disabled = !can("mark_result") || !pending;
}

function renderSmall(cs, n) {
  const can = (key) => connected && cs.allowed.includes(key);
  setMode(small, cs.mode);
  $("small-name").textContent = `작업차 ${n}`;
  setModeChip($("small-mode"), cs.mode);
  setLink($("small-link"), cs.link);
  $("small-where").textContent = whereText(cs);
  $("small-progress").textContent = progressText(cs);
  const pending = cs.result_pending && cs.result_cycle != null;
  small.classList.toggle("pending", pending && can("mark_result"));
  $("small-result-cycle").textContent = pending ? `${cs.result_cycle}회째 결과` : "";
  smallBtn.stop.disabled = !can("stop");
  smallBtn.success.disabled = !can("mark_result") || !pending;
  smallBtn.fail.disabled = !can("mark_result") || !pending;
  smallBtn.select.disabled = !can("select_car");
  smallBtn.select.textContent = `작업차 ${n} 조종`;
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

  const n = state.selected_car;
  const o = n === 1 ? 2 : 1;
  renderBig(state.cars[String(n)], n);
  renderSmall(state.cars[String(o)], o);

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

  // 손 인식 칸: 손이 보이면 초록 테두리, 안 보이면 빨강 + "손이 보이지 않음" (영상 위에 겹치지 않고 옆에)
  setVideo(handVideo, state.hand_video_url, h.service, "손 인식 서비스 꺼짐");
  const hv = $("hand-video");
  hv.classList.toggle("seen", h.service && h.detected);
  hv.classList.toggle("lost", h.service && !h.detected);
  $("hand-note").textContent = h.service && !h.detected ? "손이 보이지 않음" : "";
  const hs = $("hand-state");
  if (!h.service) { hs.textContent = "● 서비스 꺼짐"; hs.className = "dot-text bad"; }
  else if (!h.detected) { hs.textContent = "● 인식 끊김"; hs.className = "dot-text bad"; }
  else { hs.textContent = "● 인식 중"; hs.className = "dot-text good"; }
  $("hand-calib").textContent = h.calibrated ? "맞춤 완료" : "맞춤 전: 손 펴기 → 주먹 순서로 기록";
  const calibOk = connected && state.cars[String(n)].allowed.includes("hand_calib");
  $("btn-calib-open").disabled = !calibOk;
  $("btn-calib-fist").disabled = !calibOk;
}

// ------------------------------------------------------------------ 알림 (①: 바로 앞과 같으면 한 줄로 합침)

function two(n) { return String(n).padStart(2, "0"); }
function clock(t) {
  const d = new Date((t || Date.now() / 1000) * 1000);
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

function addAlert(msg) {
  const list = $("alerts");
  const empty = list.querySelector(".empty-note");
  if (empty) empty.remove();
  const key = [msg.level, msg.code, msg.car, msg.text].join("|");
  const top = list.firstElementChild;
  if (top && top.dataset.key === key) {          // 사이에 다른 알림이 없을 때만 합침
    const count = Number(top.dataset.count) + 1;
    top.dataset.count = String(count);
    top.querySelector(".time").textContent = clock(msg.t);
    top.querySelector(".count").textContent = `×${count} (처음 ${top.dataset.first})`;
    return;
  }
  const li = document.createElement("li");
  li.className = msg.level;
  li.dataset.key = key;
  li.dataset.count = "1";
  li.dataset.first = clock(msg.t);
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = li.dataset.first;
  const text = document.createElement("span");
  text.textContent = msg.text;
  text.title = msg.code;
  const count = document.createElement("span");
  count.className = "count";
  li.append(time, text, count);
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

showNoAlerts();
render();
connect();
