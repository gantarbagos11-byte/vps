

const tabSetting=document.getElementById("tabSetting"),tabDuel=document.getElementById("tabDuel"),panelSetting=document.getElementById("panelSetting"),panelDuel=document.getElementById("panelDuel");
function switchTab(name){
  const setting=name==="setting";
  panelSetting.classList.toggle("active",setting); panelDuel.classList.toggle("active",!setting);
  tabSetting.classList.toggle("active",setting); tabDuel.classList.toggle("active",!setting);
  tabSetting.setAttribute("aria-selected",String(setting)); tabDuel.setAttribute("aria-selected",String(!setting));
  window.scrollTo({top:0,behavior:"smooth"});
}
tabSetting.onclick=()=>switchTab("setting");
tabDuel.onclick=()=>switchTab("duel");
const N=10;
const clients=Array.from({length:N},()=>({ws:null,loggedIn:false,inRoom:false,pingTimer:null,intentionalClose:false,balance:null}));
const rows=document.getElementById("rows"),roomEl=document.getElementById("room"),userList=document.getElementById("userList"),targetList=document.getElementById("targetList"),userCount=document.getElementById("userCount"),targetCount=document.getElementById("targetCount"),saveLoadEl=document.getElementById("saveLoad");
const kickProgressStatus=document.getElementById("kickProgressStatus"),kickProgressDetail=document.getElementById("kickProgressDetail"),kickDispatched=document.getElementById("kickDispatched"),kickFailed=document.getElementById("kickFailed"),kickWs=document.getElementById("kickWs"),kickRate=document.getElementById("kickRate"),kickSocketReports=document.getElementById("kickSocketReports");
let kickProgressJobs=new Map();let kickProgressSeen=new Set();let kickProgressStartedAt=0;let confirmedKickedTargets=new Set();
const SAVE_PREFIX="migsock_config_";
let participantUsers=[];
const participantBySocket=Array.from({length:N},()=>[]);
let autoTargetLocked=false;
let autoTargetTimer=null;
let autoTargetCycle=0;
const COUNTDOWN_DEFAULT=60000;
let countdownDeadline=0;
let countdownTimer=null;
let lastKickEventKey="";
let countdownKickTriggered=false;
let suicideEnabled=false;
let suicideTriggeredKeys=new Set();
function countdownEl(){return document.getElementById("countdown")}
function setCountdownValue(v){countdownEl().value=String(Math.max(0,Math.round(v)))}
function kickEventData(m){const d=m?.data&&typeof m.data==="object"?m.data:m||{};return {
  type:String(m?.type??d?.type??d?.event_type??"").toLowerCase(),
  action:String(m?.action??d?.action??"").toLowerCase(),
  status:String(m?.status_message??d?.status_message??d?.message??"").toLowerCase(),
  room:String(m?.room??d?.room??"").trim(),
  target:String(m?.target_username??d?.target_username??d?.username??d?.target??"").trim(),
  time:String(m?.time??d?.time??d?.timestamp??"").trim()
}}
function isConfirmedKickEvent(m){
  const d=kickEventData(m);
  if(!d.target)return false;
  const exact=new Set([
    "room.participant.kicked","room.member.kicked","room.user.kicked",
    "participant.kicked","member.kicked","user.kicked"
  ]);
  if(exact.has(d.type))return true;
  if(d.type.includes("vote")||d.type==="room.kick.result"||d.type==="room.command.result")return false;
  return ["has been kicked","was kicked","have been kicked","kicked from the room"]
    .some(x=>d.status.includes(x));
}

function isKickEvent(m){
  const d=kickEventData(m);
  if(!d.type)return false;
  // Countdown hanya dipicu oleh event room.kick.state dengan pesan "A vote to kick".
  // Event kick lain, room.kick.result, room.kicked, atau action vote_started tanpa
  // pesan "A vote to kick" tidak boleh menyalakan countdown.
  return d.type==="room.kick.state" && d.status.includes("a vote to kick");
}
function kickEventKey(m){const d=kickEventData(m);return [d.type,d.action,d.room,d.target,d.time,d.status].join("|")}
function stopCountdown(reset=true){if(countdownTimer){clearInterval(countdownTimer);countdownTimer=null}countdownDeadline=0;if(reset)setCountdownValue(COUNTDOWN_DEFAULT)}
function resetCountdownTimer(){stopCountdown(true);lastKickEventKey="";countdownKickTriggered=false;log(0,"Countdown di-reset ke 60000 dan berhenti")}
function startCountdownFromKick(m){
  const room=kickEventData(m).room;
  const selected=roomEl.value.trim();
  if(room && selected && room.toLowerCase()!==selected.toLowerCase())return;
  const key=kickEventKey(m);
  if(key===lastKickEventKey)return;
  lastKickEventKey=key;
  countdownKickTriggered=false;
  if(countdownTimer)clearInterval(countdownTimer);
  const duration=COUNTDOWN_DEFAULT;
  const startedAt=performance.now();
  countdownDeadline=startedAt+duration;
  setCountdownValue(duration);
  log(0,`EVENT KICK terdeteksi${room?` di room ${room}`:""}. Countdown mulai ${duration} ms`);
  const tick=()=>{
    const remaining=Math.max(0,countdownDeadline-performance.now());
    setCountdownValue(remaining);
    const timerKickall=Math.max(0,Number(document.getElementById("timerKickall")?.value)||0);
    if(!countdownKickTriggered && remaining<=timerKickall){
      countdownKickTriggered=true;
      log(0,`Countdown ${remaining} ms <= Timer KickAll ${timerKickall} ms → KICKALL TERPICU`);
      kickAll();
    }
    if(remaining<=0){
      clearInterval(countdownTimer);
      countdownTimer=null;
      countdownDeadline=0;
      setCountdownValue(COUNTDOWN_DEFAULT);
      log(0,"Countdown selesai: 0 → reset 60000 dan berhenti");
    }
  };
  countdownTimer=setInterval(tick,25);
  tick();
}

function log(i,m){/* UI log removed */}
function status(i,t,c){const e=document.getElementById(`status-${i}`);e.title=t;e.setAttribute("aria-label",t);e.className="status-balloon "+c}
function setBalance(i,value){const e=document.getElementById(`balance-${i}`);if(!e)return;e.textContent=String(value??"—").replace(/\s*CR\s*$/i,"")}
function parseBalanceValue(v){
  if(v===null||v===undefined)return null;
  if(typeof v==='number'&&Number.isFinite(v))return String(v);
  if(typeof v!=='string')return null;
  const s=v.trim();if(!s)return null;
  // Accept plain numbers as well as strings such as "3 CR", "0 CR" or "balance: 15".
  const m=s.match(/-?\d+(?:[.,]\d+)?/);
  if(!m)return null;
  const n=Number(m[0].replace(',','.'));
  if(!Number.isFinite(n))return null;
  return /\bcr\b/i.test(s)?String(n):String(n/1000);
}
function extractBalance(m){
  // The API has returned wallet information in slightly different shapes across
  // session.ready/account events, so search nested wallet/balance/credit fields.
  const seen=new Set();
  const keyScore=(key)=>{
    const k=String(key||'').toLowerCase();
    if(k.includes('wallet')||k.includes('balance')||k.includes('credit'))return 3;
    return 0;
  };
  const walk=(v,key='',depth=0)=>{
    if(depth>7||v===null||v===undefined)return null;
    if(typeof v==='number'&&Number.isFinite(v)&&keyScore(key)>0)return String(v/1000);
    if(typeof v==='string'){
      const k=String(key||'').toLowerCase();
      if(k.includes('wallet')||k.includes('balance')||k.includes('credit')||/\bcr\b/i.test(v)){
        return parseBalanceValue(v);
      }
      return null;
    }
    if(typeof v!=='object')return null;
    if(seen.has(v))return null;
    seen.add(v);
    // First inspect wallet/balance/credit keys, then recurse through the rest.
    const entries=Array.isArray(v)?v.map((x,i)=>[String(i),x]):Object.entries(v);
    for(const [k,x] of entries){if(keyScore(k)>0){const out=walk(x,k,depth+1);if(out!==null)return out}}
    for(const [k,x] of entries){if(keyScore(k)===0){const out=walk(x,k,depth+1);if(out!==null)return out}}
    return null;
  };
  return walk(m);
}
function updateBalanceFromMessage(i,m){
  const type=String(m?.type??"").toLowerCase();
  if(type==="session.ready"||type.includes("wallet")||type.includes("balance")||type.includes("credit")){
    const b=extractBalance(m);if(b!==null)setBalance(i,b);
  }
}
function render(){for(let i=0;i<N;i++){insertRow(i)}}
function insertRow(i){rows.insertAdjacentHTML("beforeend",`<tr><td>${i+1}</td><td><input id="user-${i}" placeholder="Username ${i+1}" autocomplete="off"></td><td class="password-cell"><div class="password-wrap"><input id="pass-${i}" type="password" placeholder="Password ${i+1}" autocomplete="new-password"><span id="balance-${i}" class="wallet-caption" title="Saldo">—</span></div></td><td style="text-align:center"><span id="status-${i}" class="status-balloon offline" title="OFFLINE" aria-label="OFFLINE"></span></td></tr>`) }
function cred(i){return{username:document.getElementById(`user-${i}`).value.trim(),password:document.getElementById(`pass-${i}`).value}}
function getRoom(){const room=roomEl.value.trim();if(!room){return null}return room}
function getRange(){
  const av=String(document.getElementById("start").value||"").trim();
  const bv=String(document.getElementById("end").value||"").trim();
  if(!/^\d+$/.test(av)||!/^\d+$/.test(bv)){ return null; }
  const a=Number(av), b=Number(bv);
  if(!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||a<0||b<a){ return null; }
  const values=[];
  for(let i=0;i<N;i++){
    const v=(i===0)?a:(i===N-1)?b:Math.round(a+(b-a)*i/(N-1));
    values.push(v);
  }
  return {mode:"any-range",values,startWidth:av.length,endWidth:bv.length};
}
function refreshUserList(){}
function updateTargetCount(){targetCount.textContent=targetList.querySelectorAll(".target-item").length}
function targetHas(username){
  const key=String(username||"").trim().toLowerCase();
  return !!key && [...targetList.querySelectorAll(".target-item")].some(o=>String(o.dataset.username||"").trim().toLowerCase()===key);
}
function removeConfirmedKickedTarget(username){
  username=String(username||"").trim();
  if(!username)return false;
  const key=username.toLowerCase();
  confirmedKickedTargets.add(key);

  // Remove from TARGET immediately.
  [...targetList.querySelectorAll(".target-item")].forEach(row=>{
    const value=String(row.dataset.username||row.textContent||"").trim().toLowerCase();
    if(value===key)row.remove();
  });

  // Purge every participant snapshot first. A later WS response must not
  // resurrect this username.
  participantUsers=(participantUsers||[]).filter(u=>String(u||"").trim().toLowerCase()!==key);
  if(Array.isArray(participantBySocket)){
    for(let i=0;i<participantBySocket.length;i++){
      const list=Array.isArray(participantBySocket[i])?participantBySocket[i]:[];
      participantBySocket[i]=list.filter(u=>String(u||"").trim().toLowerCase()!==key);
    }
  }

  // Remove every possible USER row, including future/legacy row markup.
  [...userList.querySelectorAll(".user-item")].forEach(row=>{
    const value=String(row.dataset.username||row.querySelector("span")?.textContent||row.textContent||"").trim().toLowerCase();
    if(value===key)row.remove();
  });

  // Rebuild USER from the cleaned snapshot so the count and empty-state stay
  // consistent, while TARGET entries are preserved.
  const cleaned=[...new Map((participantBySocket||[]).flat()
    .map(x=>String(x||"").trim()).filter(Boolean)
    .map(u=>[u.toLowerCase(),u])).values()]
    .filter(u=>!confirmedKickedTargets.has(u.toLowerCase()));
  participantUsers=cleaned;
  showUsers(cleaned,false);

  updateTargetCount();
  log(0,`KICKED: ${username} dihapus dari USER dan TARGET; snapshot WS dibersihkan`);
  return true;
}

function addTarget(username){
  const value=String(username||"").trim();
  if(!value)return false;
  const key=value.toLowerCase();
  const ownWs=new Set();
  for(let i=0;i<N;i++){const u=cred(i).username;if(u)ownWs.add(u.trim().toLowerCase());}
  if(ownWs.has(key)||confirmedKickedTargets.has(key)||targetHas(value)||targetList.querySelectorAll(".target-item").length>=10)return false;
  const e=document.createElement("div");
  e.className="target-item";
  e.dataset.username=value;
  e.textContent=value;
  targetList.appendChild(e);
  updateTargetCount();
  return true;
}
function moveUserToTarget(username,row,checkbox){
  const added=addTarget(username);
  if(added){
    if(row&&row.parentNode)row.parentNode.removeChild(row);
    userCount.textContent=userList.querySelectorAll(".user-item").length;
  }else if(checkbox){
    checkbox.checked=false;
  }
}
function autoAddMatchingTargets(users){
  const blockedWsUsers=new Set();
  for(let i=0;i<N;i++){
    const u=cred(i).username;
    if(u)blockedWsUsers.add(u.trim().toLowerCase());
  }

  const list=[...new Map((users||[])
    .map(x=>String(x||'').trim())
    .filter(Boolean)
    .map(u=>[u.toLowerCase(),u])).values()]
    .filter(u=>!blockedWsUsers.has(u.toLowerCase())&&!confirmedKickedTargets.has(u.toLowerCase()));

  /*
   * Auto Target v2:
   * - only compares usernames inside the same prefix/suffix family;
   * - numeric and alphabetic sequences are handled separately;
   * - a family containing 10 members is strongly preferred;
   * - when a family has >10 members, choose the tightest 10-number window;
   * - when exactly 10 similar members exist but they are not perfectly
   *   consecutive, they are still accepted instead of being ignored;
   * - plain base names are never mixed with numbered variants;
   * - WS1..WS10 usernames and already-kicked users are always excluded.
   */
  const numericGroups=new Map();
  const alphaGroups=new Map();
  const add=(map,key,item)=>{
    if(!map.has(key))map.set(key,[]);
    map.get(key).push(item);
  };

  for(const u of list){
    let m=u.match(/^(.*?)(\d+)(.*?)$/);
    if(m){
      add(numericGroups,`${m[1].toLowerCase()}|${m[3].toLowerCase()}`,{
        u,n:Number(m[2]),raw:m[2],width:m[2].length
      });
      continue;
    }
    m=u.match(/^(.*?)([A-Za-z])$/);
    if(m){
      add(alphaGroups,m[1].toLowerCase(),{
        u,ch:m[2].toLowerCase(),code:m[2].toLowerCase().charCodeAt(0)
      });
    }
  }

  const candidates=[];

  // Numeric families.
  for(const [key,rawItems] of numericGroups){
    const byNumber=new Map();
    for(const x of rawItems){
      const old=byNumber.get(x.n);
      if(!old || x.width>old.width || x.u.localeCompare(old.u)<0)byNumber.set(x.n,x);
    }
    const items=[...byNumber.values()].sort((a,b)=>a.n-b.n||a.u.localeCompare(b.u));
    if(items.length<2)continue;

    // Find the densest 10-member window. For exactly 10 members, keep the
    // whole family even if a number is missing from the sequence.
    let bestWindow=null;
    let bestScore=null;

    if(items.length>=10){
      for(let i=0;i<=items.length-10;i++){
        const window=items.slice(i,i+10);
        let contiguous=1;
        for(let k=1;k<window.length;k++){
          if(window[k].n===window[k-1].n+1)contiguous++;
          else break;
        }
        let run=1, maxRun=1;
        for(let k=1;k<window.length;k++){
          if(window[k].n===window[k-1].n+1)run++;
          else run=1;
          if(run>maxRun)maxRun=run;
        }
        const span=window[9].n-window[0].n;
        const sameWidth=window.filter(x=>x.width===window[0].width).length;
        const score={
          full:10,
          contiguous,
          maxRun,
          span,
          sameWidth,
          start:window[0].n
        };
        if(!bestScore ||
          score.contiguous>bestScore.contiguous ||
          (score.contiguous===bestScore.contiguous && score.maxRun>bestScore.maxRun) ||
          (score.contiguous===bestScore.contiguous && score.maxRun===bestScore.maxRun && score.span<bestScore.span) ||
          (score.contiguous===bestScore.contiguous && score.maxRun===bestScore.maxRun && score.span===bestScore.span && score.sameWidth>bestScore.sameWidth) ||
          (score.contiguous===bestScore.contiguous && score.maxRun===bestScore.maxRun && score.span===bestScore.span && score.sameWidth===bestScore.sameWidth && score.start<bestScore.start)){
          bestWindow=window;
          bestScore=score;
        }
      }
    }else{
      // Keep the previous useful behaviour for smaller families: they are
      // candidates only when there is a genuine consecutive sequence.
      for(let i=0;i<items.length;i++){
        let run=[items[i]];
        for(let j=i+1;j<items.length;j++){
          if(items[j].n===run[run.length-1].n+1)run.push(items[j]);
          else break;
        }
        if(run.length>=2 && (!bestWindow || run.length>bestWindow.length ||
          (run.length===bestWindow.length && run[0].n<bestWindow[0].n))){
          bestWindow=run;
        }
      }
      if(bestWindow)bestScore={
        full:bestWindow.length,
        contiguous:bestWindow.length,
        maxRun:bestWindow.length,
        span:bestWindow[bestWindow.length-1].n-bestWindow[0].n,
        sameWidth:bestWindow.filter(x=>x.width===bestWindow[0].width).length,
        start:bestWindow[0].n
      };
    }

    if(!bestWindow||bestWindow.length<2)continue;
    const selected=bestWindow.slice(0,10);
    candidates.push({
      items:selected,
      score:400000 +
        (selected.length===10?100000:0) +
        selected.length*1000 +
        bestScore.contiguous*100 +
        bestScore.maxRun*50 -
        Math.min(bestScore.span,9999) -
        Math.min(bestScore.start,999999)/1000000,
      type:'num',
      start:bestScore.start,
      key
    });
  }

  // Alphabetic families such as abcd.a ... abcd.j.
  for(const [key,rawItems] of alphaGroups){
    const byCode=new Map();
    for(const x of rawItems)if(!byCode.has(x.code))byCode.set(x.code,x);
    const items=[...byCode.values()].sort((a,b)=>a.code-b.code||a.u.localeCompare(b.u));
    if(items.length<2)continue;

    let bestWindow=null,bestScore=null;
    if(items.length>=10){
      for(let i=0;i<=items.length-10;i++){
        const window=items.slice(i,i+10);
        let maxRun=1,run=1;
        for(let k=1;k<window.length;k++){
          if(window[k].code===window[k-1].code+1)run++;
          else run=1;
          if(run>maxRun)maxRun=run;
        }
        const span=window[9].code-window[0].code;
        const score={contiguous:maxRun,span,start:window[0].code};
        if(!bestScore||score.contiguous>bestScore.contiguous||
          (score.contiguous===bestScore.contiguous&&score.span<bestScore.span)||
          (score.contiguous===bestScore.contiguous&&score.span===bestScore.span&&score.start<bestScore.start)){
          bestWindow=window;bestScore=score;
        }
      }
    }else{
      for(let i=0;i<items.length;i++){
        let run=[items[i]];
        for(let j=i+1;j<items.length;j++){
          if(items[j].code===run[run.length-1].code+1)run.push(items[j]);
          else break;
        }
        if(run.length>=2&&(!bestWindow||run.length>bestWindow.length||
          (run.length===bestWindow.length&&run[0].code<bestWindow[0].code)))bestWindow=run;
      }
      if(bestWindow)bestScore={
        contiguous:bestWindow.length,
        span:bestWindow[bestWindow.length-1].code-bestWindow[0].code,
        start:bestWindow[0].code
      };
    }

    if(!bestWindow||bestWindow.length<2)continue;
    candidates.push({
      items:bestWindow.slice(0,10),
      score:300000 +
        (bestWindow.length===10?100000:0) +
        bestWindow.length*1000 +
        bestScore.contiguous*100 -
        bestScore.span -
        bestScore.start/100000,
      type:'alpha',
      start:bestScore.start,
      key
    });
  }

  candidates.sort((a,b)=>b.score-a.score);
  const best=candidates[0];
  if(!best)return [];

  const capacity=Math.max(0,10-targetList.querySelectorAll('.target-item').length);
  if(!capacity)return [];

  const added=[];
  for(const x of best.items.slice(0,capacity)){
    if(addTarget(x.u))added.push(x.u);
  }
  return added;
}
function clearTargets(){
  if(autoTargetTimer){clearTimeout(autoTargetTimer);autoTargetTimer=null}
  autoTargetCycle++;
  const restored=[...new Set(participantUsers)].filter(u=>!confirmedKickedTargets.has(String(u).toLowerCase()));
  targetList.innerHTML="";
  autoTargetLocked=false;
  updateTargetCount();
  if(restored.length) showUsers(restored,false); else { userList.innerHTML=""; userCount.textContent="0"; }
  log(0,`TARGET dihapus: ${restored.length} username dikembalikan ke USER`);
}
function generateTroop(){
  const prefix=document.getElementById("nametroop").value.trim();
  if(!prefix){return}
  const range=getRange();
  if(!range)return;
  for(let i=0;i<N;i++){
    document.getElementById(`user-${i}`).value="";
    document.getElementById(`pass-${i}`).value="";
  }

  if(range.mode==="any-range"){
    range.values.forEach((n,slot)=>{
      const width=Math.max(range.startWidth,1);
      const suffix=String(n).padStart(width,"0");
      const u=prefix+suffix;
      document.getElementById(`user-${slot}`).value=u;
      log(slot,`Username ${u}`);
    });
  }
  refreshUserList();
}
function setPass(){
  const pw=document.getElementById("passwordAll").value;
  if(!pw){return}
  for(let i=0;i<N;i++)document.getElementById(`pass-${i}`).value=pw;
  log(0,`Password diisi untuk ${N} ID`);
}
function send(i,o,hideLog=false){const c=clients[i];if(!c.ws||c.ws.readyState!==WebSocket.OPEN){log(i,"Socket belum OPEN");return false}c.ws.send(JSON.stringify(o));if(hideLog)log(i,`SEND ${o.type}`);else log(i,"SEND "+JSON.stringify(o));return true}
function stopPing(i){if(clients[i].pingTimer){clearInterval(clients[i].pingTimer);clients[i].pingTimer=null}}
function startPing(i){stopPing(i);clients[i].pingTimer=setInterval(()=>{if(clients[i].loggedIn)send(i,{type:"ping"},true)},40000)}
function logoutOne(i){const c=clients[i];c.intentionalClose=true;stopPing(i);c.loggedIn=false;c.inRoom=false;c.balance=null;setBalance(i,"—");if(c.ws)try{c.ws.close(1000,"logout")}catch(e){}status(i,"OFFLINE","offline");log(i,"Logout")}
function loginAll(){for(let i=0;i<N;i++){const x=cred(i);if(x.username&&x.password)setTimeout(()=>loginOne(i),i*200)}}
function logoutAll(){resetKickLive();for(let i=0;i<N;i++)logoutOne(i)}
function enterAll(){const room=getRoom();if(!room)return;for(let i=0;i<N;i++)if(clients[i].loggedIn)send(i,{type:"room.join",room},true)}
function leaveAll(){const room=getRoom();if(!room)return;resetKickLive();for(let i=0;i<N;i++)if(clients[i].loggedIn)send(i,{type:"room.leave",room},true)}
function extractUsers(m){
  let arr=m?.participants??m?.data?.participants??m?.users??m?.data?.users??m?.data?.members??m?.data?.list??m?.data?.results??m?.result?.participants??m?.result?.users??[];
  if(!Array.isArray(arr)&&arr&&typeof arr==='object')arr=Object.values(arr);
  if(!Array.isArray(arr))arr=[];
  return arr.map(x=>typeof x==='string'?x:(x?.username??x?.user??x?.name??x?.nickname??x?.id??"" )).filter(Boolean);
}
function showUsers(users,auto=true){
  const uniq=[...new Map((users||[])
    .map(x=>String(x||'').trim())
    .filter(Boolean)
    .map(u=>[u.toLowerCase(),u])).values()];
  participantUsers=uniq;
  userList.innerHTML="";

  // Auto Target is intentionally locked after the first successful selection
  // for the current List User cycle. Later WS participant responses only
  // refresh the User list and cannot replace the chosen pattern.
  // Participant responses can arrive from WS1..WS10 at different times.
  // Auto Target is scheduled by handleParticipants after the merged snapshot
  // has had time to settle, so an early partial response cannot lock the
  // wrong pattern.

  const visible=uniq.filter(u=>!targetHas(u)&&!confirmedKickedTargets.has(u.toLowerCase()));
  if(!visible.length){
    const e=document.createElement("div");e.className="empty";
    e.textContent=uniq.length?"Semua participant sudah dipindahkan ke TARGET":"Tidak ada participant";
    userList.appendChild(e);
  }else{
    visible.forEach(u=>{
      const e=document.createElement("div");e.className="user-item";
      const cb=document.createElement("input");cb.type="checkbox";cb.className="user-check";cb.title="Pindahkan ke TARGET";
      const label=document.createElement("span");label.textContent=u;
      cb.onchange=()=>{if(cb.checked)moveUserToTarget(u,e,cb)};
      e.append(cb,label);userList.appendChild(e);
    });
  }
  userCount.textContent=visible.length;
  updateTargetCount();
}
function scheduleAutoTarget(merged,cycle){
  if(autoTargetLocked||!merged||!merged.length)return;
  if(autoTargetTimer)clearTimeout(autoTargetTimer);

  // Wait for a quiet period after the latest WS participant response.
  // This prevents WS1 returning first from locking a partial 2/3-member
  // pattern before WS2..WS10 have contributed their usernames.
  autoTargetTimer=setTimeout(()=>{
    if(cycle!==autoTargetCycle||autoTargetLocked)return;
    const current=[...new Map(participantBySocket.flat()
      .map(x=>String(x||'').trim())
      .filter(Boolean)
      .map(u=>[u.toLowerCase(),u])).values()];
    const autoTargets=autoAddMatchingTargets(current);
    if(autoTargets.length){
      autoTargetLocked=true;
      log(0,`AUTO TARGET: ${autoTargets.join(", ")}`);
      showUsers(current,false);
    }
  },700);
}
function listUser(){
  const room=getRoom();if(!room)return;
  if(autoTargetTimer){clearTimeout(autoTargetTimer);autoTargetTimer=null}
  targetList.innerHTML="";
  userList.innerHTML="";
  userCount.textContent="0";
  autoTargetLocked=false;
  autoTargetCycle++;
  participantUsers=[];
  for(let i=0;i<N;i++)participantBySocket[i]=[];
  updateTargetCount();
  log(0,"LISTUSER: TARGET dan snapshot participant dikosongkan");
  let sent=0;
  for(let i=0;i<N;i++)if(clients[i].loggedIn){if(send(i,{type:"room.participants",room},true))sent++}
  if(!sent)return;
}
function listRoom(){listUser()}
function handleParticipants(m,i){
  if(m.type!=="room.participants.result"&&m.type!=="room.participants")return false;
  const users=extractUsers(m);
  participantBySocket[i]=users;
  const merged=[...new Map(participantBySocket.flat()
    .map(x=>String(x||'').trim())
    .filter(Boolean)
    .map(u=>[u.toLowerCase(),u])).values()];
  showUsers(merged,false);
  scheduleAutoTarget(merged,autoTargetCycle);
  log(i,`LISTROOM: WS${i+1} ${users.length} user | gabungan ${merged.length}`);
  return true;
}
// Patch each socket handler to also process room participant results.
function loginOne(i){const c=clients[i],x=cred(i);if(!x.username||!x.password){log(i,"Username/password kosong");return}c.intentionalClose=false;c.loggedIn=false;c.inRoom=false;c.balance=null;setBalance(i,"—");stopPing(i);if(c.ws)try{c.ws.close()}catch(e){}status(i,"CONNECTING","connecting");log(i,"Connecting local proxy...");const ws=new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);c.ws=ws;ws.onopen=()=>log(i,"Proxy OPEN; menunggu auth.required");ws.onmessage=e=>{let m;try{m=JSON.parse(e.data)}catch(_){log(i,"RECV non-JSON");return}updateBalanceFromMessage(i,m);if(m.type==="kick.target.confirmed"){removeConfirmedKickedTarget(m.target_username);return}if(isConfirmedKickEvent(m)){removeConfirmedKickedTarget(kickEventData(m).target);return}if(m.type==="kick.progress"){handleKickProgress(m);return}if(m.type==="proxy.error"){status(i,"PROXY ERROR","error");log(i,"PROXY ERROR: "+(m.data?.message||"unknown"));return}if(isKickEvent(m)){maybeSuicide(m);startCountdownFromKick(m)}if(handleParticipants(m,i))return;if(m.type==="auth.required"){log(i,"RECV auth.required");send(i,{type:"developer.login",username:x.username,password:x.password},true)}else if(m.type==="session.ready"){c.loggedIn=true;c.inRoom=false;status(i,"ONLINE","online");startPing(i);log(i,"LOGIN OK")}else if(m.type==="room.join.result"){const ok=m.ok===true||m.data?.ok===true||m.data?.joined===true||m.success===true;if(ok){c.inRoom=true;log(i,"ENTER ROOM OK");const r=roomEl.value.trim();if(r)send(i,{type:"room.participants",room:r},true)}else log(i,"ENTER ROOM RESULT "+JSON.stringify(m))}else if(m.type==="room.leave.result"){const ok=m.ok===true||m.data?.ok===true||m.data?.left===true||m.success===true;if(ok){c.inRoom=false;log(i,"LEAVE ROOM OK")}else log(i,"LEAVE ROOM RESULT "+JSON.stringify(m))}else if(m.type==="session.replaced"){c.loggedIn=false;c.inRoom=false;stopPing(i);status(i,"REPLACED","error")}else if(m.type==="error"){c.loggedIn=false;c.inRoom=false;stopPing(i);status(i,"ERROR","error");log(i,"API ERROR "+JSON.stringify(m))}};ws.onerror=()=>{status(i,"ERROR","error");log(i,"Local WebSocket proxy error")};ws.onclose=e=>{stopPing(i);c.loggedIn=false;c.inRoom=false;if(!c.intentionalClose)status(i,"CLOSED","offline");log(i,`CLOSED code=${e.code} reason=${e.reason||"-"}`)}};
function syncBruteCombo(source){
  const brute=document.getElementById('burstKick');
  const combo=document.getElementById('comboKick');
  if(source==='combo' && combo.value!=='off') brute.value='0';
  if(source==='brute' && brute.value!=='0') combo.value='off';
}

document.getElementById('burstKick').addEventListener('change',()=>syncBruteCombo('brute'));
document.getElementById('comboKick').addEventListener('change',()=>syncBruteCombo('combo'));

async function sandboxKick(){
  const targets=[...targetList.querySelectorAll('.target-item')].map(e=>e.dataset.username).filter(Boolean).slice(0,10);
  if(!targets.length)return;
  const cfg={targets,loop:Math.max(1,Math.min(100,Number(document.getElementById('loopKick').value)||1)),delayTarget:Math.max(0,Number(document.getElementById('delayTarget').value)||0),delayBatch:Math.max(0,Number(document.getElementById('delayBatch').value)||0),sockets:10};
  try{
    resetKickLive();
    const r=await fetch('/api/sandbox-kick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    const d=await r.json().catch(()=>({}));
    if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);
    log(0,`SANDBOX selesai: ${d.totalJobs??0} simulasi | ${d.elapsedMs??0} ms`);
  }catch(e){log(0,'SANDBOX ERROR: '+e.message)}
}

async function kickAll(){
  const room=getRoom();
  if(!room)return;
  const targets=[...targetList.querySelectorAll('.target-item')].map(e=>e.dataset.username).filter(Boolean).slice(0,10);
  if(!targets.length){return}
  const bruteEl=document.getElementById('burstKick');
  const comboEl=document.getElementById('comboKick');
  syncBruteCombo(comboEl.value!=='off'?'combo':'brute');
  const combo=comboEl.value;
  const brute=Math.min(10,Math.max(1,Number(bruteEl.value)||1));
  const cfg={room,targets,loop:Math.max(1,Number(document.getElementById('loopKick').value)||1),delayTarget:Math.max(0,Number(document.getElementById('delayTarget').value)||0),delayBatch:Math.max(0,Number(document.getElementById('delayBatch').value)||0),burst:brute,combo};
  if(combo==='combo1') log(0,`KICKALL COMBO1: BRUTE2 + BRUTE3 berjalan bersamaan & berselingan | ${targets.length} target | loop=${cfg.loop}`);
  else if(combo==='combo2') log(0,`KICKALL COMBO2: BRUTE3 + BRUTE4 berjalan bersamaan & berselingan | ${targets.length} target | loop=${cfg.loop}`);
  else log(0,`KICKALL mulai: ${targets.length} target | loop=${cfg.loop} | burst=${cfg.burst} | delayTarget=${cfg.delayTarget} | delayBatch=${cfg.delayBatch}`);
  try{
    const r=await fetch('/api/kick-loop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    const d=await r.json().catch(()=>({}));
    if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);
    log(0,`KICKALL selesai/dijadwalkan: ${d.totalJobs??0} dispatch | WS aktif ${d.websockets??0}`);
  }catch(e){log(0,'KICKALL ERROR: '+e.message)}
}
function setSuicide(enabled){
  suicideEnabled=!!enabled;
  const b=document.getElementById('suicideToggle');
  b.textContent=suicideEnabled?'☠️ SUICIDE ON':'☠️ SUICIDE OFF';
  b.className=suicideEnabled?'suicide-on':'suicide-off';
  log(0,`SUICIDE ${suicideEnabled?'ON':'OFF'}`);
}
function toggleSuicide(){setSuicide(!suicideEnabled)}
function maybeSuicide(m){
  if(!suicideEnabled)return;
  const d=kickEventData(m);
  if(d.type!=='room.kick.state'||d.action!=='vote_started'||!d.target)return;
  const room=d.room||roomEl.value.trim();
  if(!room)return;
  const own=new Set();
  for(let i=0;i<N;i++){const u=cred(i).username;if(u)own.add(u.toLowerCase())}
  if(!own.has(d.target.toLowerCase()))return;
  const key=[room.toLowerCase(),d.target.toLowerCase(),d.time||d.status].join('|');
  if(suicideTriggeredKeys.has(key))return;
  suicideTriggeredKeys.add(key);
  if(suicideTriggeredKeys.size>100)suicideTriggeredKeys=new Set([...suicideTriggeredKeys].slice(-50));
  log(0,`SUICIDE: vote_started menarget ${d.target}; kirim suicide ke WS aktif`);
  fetch('/api/suicide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({room,target_username:d.target,key})}).then(async r=>{const x=await r.json().catch(()=>({}));if(!r.ok||x.ok===false)throw new Error(x.error||`HTTP ${r.status}`);log(0,`SUICIDE diproses: ${d.target} | WS aktif ${x.websockets??0}`)}).catch(e=>log(0,'SUICIDE ERROR: '+e.message));
}
function renderKickSocketReports(reports, phase){
  if(!Array.isArray(reports))return;
  kickSocketReports.innerHTML="";
  reports.forEach(r=>{
    const total=Math.max(0,Number(r.totalJobs)||0);
    const done=Math.max(0,Number(r.dispatchedJobs)||0);
    const failed=Math.max(0,Number(r.failedJobs)||0);
    const completed=Math.min(total,done+failed);
    const pct=total?Math.min(100,(completed/total)*100):0;
    const state=phase==="done"?(failed?"DONE / FAIL":"DONE"):(completed?"LIVE":"READY");
    const stateClass=failed&&phase==="done"?"kick-progress-error":(phase==="done"?"kick-progress-done":"kick-progress-live");
    const el=document.createElement("div");
    el.className="kick-socket";
    el.innerHTML=`<div class="kick-socket-head"><span class="kick-socket-name" title="${escapeHtml(String(r.websocket||"WS"))}">${escapeHtml(String(r.websocket||"WS"))}</span><span class="kick-socket-state ${stateClass}">${state}</span></div><div class="kick-socket-track"><div class="kick-socket-fill" style="width:${pct.toFixed(2)}%"></div></div><div class="kick-socket-detail"><div class="kick-socket-stat"><b>${completed} / ${total}</b><span>PROGRESS</span></div><div class="kick-socket-stat"><b>${done}</b><span>OK</span></div><div class="kick-socket-stat"><b>${failed}</b><span>FAIL</span></div></div><div class="kick-socket-last" title="${escapeHtml(String(r.lastTarget||"-"))}">TARGET: ${escapeHtml(String(r.lastTarget||"-"))} · LOOP: ${Number(r.lastLoop)||0}</div>`;
    kickSocketReports.appendChild(el);
  });
}
function escapeHtml(v){return v.replace(/[&<>'"]/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[ch]))}
function resetKickLive(){
  kickProgressJobs.clear();
  kickProgressSeen.clear();
  kickProgressStartedAt=0;
  if(kickSocketReports)kickSocketReports.innerHTML="";
  if(kickProgressStatus){kickProgressStatus.textContent="IDLE";kickProgressStatus.className="kick-progress-done";}
  if(kickProgressDetail)kickProgressDetail.textContent="0 / 0";
  if(kickDispatched)kickDispatched.textContent="0";
  if(kickFailed)kickFailed.textContent="0";
  if(kickWs)kickWs.textContent="0";
  if(kickRate)kickRate.textContent="0/s";
}
function handleKickProgress(m){
  if(!m||m.type!=="kick.progress"||!m.jobId)return;
  const ss=m.socketStats||{};
  const sig=[m.jobId,m.phase,m.dispatchedJobs||0,m.failedJobs||0,m.websocket||"",ss.dispatchedJobs||0,ss.failedJobs||0,ss.lastTarget||""].join("|");
  if(kickProgressSeen.has(sig))return;
  kickProgressSeen.add(sig);
  if(kickProgressSeen.size>5000)kickProgressSeen=new Set([...kickProgressSeen].slice(-2500));
  const total=Math.max(0,Number(m.totalJobs)||0);
  const done=Math.max(0,Number(m.dispatchedJobs)||0);
  const failed=Math.max(0,Number(m.failedJobs)||0);
  if(m.phase==="started"){
    kickProgressStartedAt=performance.now();
    kickProgressJobs.clear();
    renderKickSocketReports(m.socketReports||[],"started");
  }else if(m.phase==="progress"){
    const reports=[...kickProgressJobs.values()];
    const existing=kickProgressJobs.get(String(m.websocket||""))||{websocket:m.websocket,totalJobs:Math.max(0,Number(m.socketStats?.totalJobs)||0),dispatchedJobs:0,failedJobs:0,lastTarget:"",lastLoop:0};
    if(m.socketStats)kickProgressJobs.set(String(m.websocket||""),{...existing,...m.socketStats});
    else kickProgressJobs.set(String(m.websocket||""),{...existing,dispatchedJobs:existing.dispatchedJobs+1,lastTarget:m.target||"",lastLoop:m.loop||0});
    renderKickSocketReports([...kickProgressJobs.values()],"progress");
  }else if(m.phase==="done"){
    kickProgressJobs.clear();
    (m.socketReports||[]).forEach(r=>kickProgressJobs.set(String(r.websocket||""),r));
    renderKickSocketReports([...kickProgressJobs.values()],"done");
  }
  const elapsed=Math.max(0.001,(performance.now()-kickProgressStartedAt)/1000);
  const rate=Math.round((done+failed)/elapsed);
  if(kickProgressDetail)kickProgressDetail.textContent=(done+failed)+" / "+total;
  if(kickDispatched)kickDispatched.textContent=done;
  if(kickFailed)kickFailed.textContent=failed;
  if(kickWs)kickWs.textContent=Number(m.websockets)||0;
  if(kickRate)kickRate.textContent=rate+"/s";
  if(m.phase==="started"){
    kickProgressStatus.textContent="RUNNING";kickProgressStatus.className="kick-progress-live";
  }else if(m.phase==="done"){
    kickProgressStatus.textContent=failed?"DONE / FAIL":"DONE";
    kickProgressStatus.className=failed?"kick-progress-error":"kick-progress-done";
  }else{
    kickProgressStatus.textContent="LIVE";kickProgressStatus.className="kick-progress-live";
  }
}

function safeDownloadName(name){
  let n=String(name||"migsock").trim().replace(/[^a-zA-Z0-9._ -]/g,"_");
  if(!n)n="migsock";
  if(!/\.json$/i.test(n))n+=".json";
  return n;
}
function collectConfig(){
  const accounts=[];
  for(let i=0;i<N;i++){
    accounts.push({
      username:document.getElementById(`user-${i}`).value,
      password:document.getElementById(`pass-${i}`).value
    });
  }
  return {
    version:2,
    nameTroop:document.getElementById("nametroop").value,
    start:Number(document.getElementById("start").value),
    end:Number(document.getElementById("end").value),
    passwordAll:document.getElementById("passwordAll").value,
    accounts
  };
}
function saveConfig(){
  const name=saveLoadEl.value.trim();
  if(!name){return}
  const range=getRange();
  if(!range)return;
  const cfg=collectConfig();
  const blob=new Blob([JSON.stringify(cfg,null,2)],{type:"application/json"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;
  a.download=safeDownloadName(name);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function applyConfig(cfg){
  const accounts=Array.isArray(cfg.accounts)?cfg.accounts:[];
  const start=String(cfg.start??"01"), end=String(cfg.end??"10");
  const a=Number(start), b=Number(end);
  if(!/^\d+$/.test(start)||!/^\d+$/.test(end)||!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||b-a+1!==N)
    throw new Error(`File harus berisi rentang tepat ${N} ID.`);
  document.getElementById("nametroop").value=cfg.nameTroop||"";
  document.getElementById("start").value=start;
  document.getElementById("end").value=end;
  document.getElementById("passwordAll").value=cfg.passwordAll||"";
  for(let i=0;i<N;i++){
    const a=accounts[i]||{};
    document.getElementById(`user-${i}`).value=String(a.username||"");
    document.getElementById(`pass-${i}`).value=String(a.password||"");
  }
  refreshUserList();
}
function loadConfig(){
  document.getElementById("loadFile").click();
}
document.getElementById("loadFile").addEventListener("change",async function(e){
  const file=e.target.files?.[0];
  if(!file)return;
  try{
    const cfg=JSON.parse(await file.text());
    applyConfig(cfg);
    document.getElementById("saveLoad").value=file.name.replace(/\.json$/i,"");
  }catch(err){
    log(0,"LOAD gagal: "+(err?.message||"File JSON tidak valid."));
  }finally{
    e.target.value="";
  }
});
document.getElementById("loginAll").onclick=loginAll;document.getElementById("logoutAll").onclick=logoutAll;document.getElementById("enterAll").onclick=enterAll;document.getElementById("leaveAll").onclick=leaveAll;document.getElementById("generateTroop").onclick=generateTroop;document.getElementById("setPass").onclick=setPass;document.getElementById("listRoom").onclick=listUser;document.getElementById("saveConfig").onclick=saveConfig;document.getElementById("loadConfig").onclick=loadConfig;document.getElementById("clearTarget").onclick=clearTargets;document.getElementById("kickAll").onclick=kickAll;document.getElementById("suicideToggle").onclick=toggleSuicide;document.getElementById("resetCountdown").onclick=resetCountdownTimer;document.getElementById("sandboxKick").onclick=sandboxKick;render();refreshUserList();updateTargetCount();
