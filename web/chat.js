/* Optional user-hosted chat. No discovery, sockets, fetches or timers while closed/off. */
(() => {
  "use strict";
  const state = {open:false, token:"", signature:"", expires:0, room:"general", cursor:0, messages:new Map(), controllers:new Set(), timer:0, generation:0, failures:0, connecting:null};
  let panel;
  const id = name => panel.querySelector("#lfChat" + name);
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(20)), n=>n.toString(16).padStart(2,"0")).join("");
  const clientId = () => {try {let value=localStorage.getItem("last-frequency.chat.client");if(!value){value=uuid();localStorage.setItem("last-frequency.chat.client",value);}return value;}catch(_){return uuid();}};
  const cfg = () => S.settings || {};
  const active = () => state.open && !document.hidden && cfg().chat_enabled === true;
  const status = message => {id("Status").textContent = message;};
  const stop = () => {state.generation++;state.connecting=null;clearTimeout(state.timer);state.timer=0;for(const controller of state.controllers)controller.abort();state.controllers.clear();};
  const resetSession = () => {state.token="";state.expires=0;state.signature="";state.cursor=0;state.messages.clear();};

  async function request(path, method="GET", body) {
    if(!active())throw new Error("Чат приостановлен");
    const controller=new AbortController();state.controllers.add(controller);
    const timeout=setTimeout(()=>controller.abort(),10000);
    try {
      const response=await fetch(tk(path),{method,headers:{"Content-Type":"application/json","X-Chat-Session":state.token},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
      const data=await response.json();
      if(!response.ok){const error=new Error(data.error||"Ошибка чата");error.status=response.status;throw error;}
      return data;
    } finally {clearTimeout(timeout);state.controllers.delete(controller);}
  }

  function createPanel() {
    if(panel)return;
    panel=document.createElement("section");panel.id="lfChatPanel";panel.className="lf-chat hidden";panel.setAttribute("aria-label","Чаты NOT soundLARP 100%");
    panel.innerHTML=`<div class="lf-chat-card"><header class="lf-chat-head"><div><h2>Чаты</h2><p>Музыка и переписка с друзьями</p></div><button class="iconbtn" id="lfChatClose" aria-label="Закрыть чат">✕</button></header>
      <details class="lf-chat-settings" open><summary>Подключение и ник</summary><form id="lfChatConfig" autocomplete="off"><label class="lf-chat-toggle"><input type="checkbox" id="lfChatEnabled">Включить чат</label>
      <div class="lf-chat-fields"><label>Твой ник<input class="text" id="lfChatNickname" minlength="2" maxlength="32" placeholder="Ник для переписки"></label><label>Режим<select class="select" id="lfChatMode"><option value="join">Подключиться к другу</option><option value="host">Создать чат на этом компьютере</option></select></label>
      </div><div id="lfChatJoinPanel" class="lf-chat-invite"><label for="lfChatInviteInput">Добавить чат по коду</label><textarea class="text" id="lfChatInviteInput" rows="2" maxlength="4096" spellcheck="false" placeholder="Вставь приглашение LFCHAT1.… от друга"></textarea><div class="lf-chat-config-actions"><button class="btn primary" type="button" id="lfChatJoinInvite">Добавить чат</button><span>Введи ник выше. Адрес и код доступа подставятся автоматически.</span></div><p id="lfChatJoinNotice" class="lf-chat-hint" role="status"></p></div>
      <div id="lfChatHostPanel" class="lf-chat-invite" hidden><div class="lf-chat-fields"><label id="lfChatPortRow">Порт хоста<input class="text" id="lfChatPort" type="number" min="1024" max="65535" value="8786"></label><label>Адрес для друга<select class="select" id="lfChatInviteAddress" aria-describedby="lfChatInviteHint"><option value="custom">Внешний / VPN-адрес…</option></select></label></div><label id="lfChatInviteCustomRow" class="lf-chat-invite-custom">Внешний / VPN-адрес<input class="text" id="lfChatInviteCustom" maxlength="2000" placeholder="http://доступный-IP:8786 или https://адрес"></label><p id="lfChatInviteHint" class="lf-chat-hint"></p><button class="btn" type="button" id="lfChatCopyInvite">Копировать приглашение</button><label id="lfChatInviteOutputRow" class="lf-chat-invite-result" hidden>Код приглашения<input class="text" id="lfChatInviteOutput" readonly spellcheck="false" aria-label="Код приглашения для копирования"></label><p id="lfChatInviteNotice" class="lf-chat-hint" role="status"></p><p class="lf-chat-hint">Приглашение содержит доступ к чату. Передавай его только тем, кого хочешь пригласить.</p></div>
      <details class="lf-chat-manual" id="lfChatManual"><summary id="lfChatManualTitle">Подключиться вручную</summary><div class="lf-chat-fields"><label id="lfChatAddressRow">Адрес друга<input class="text" id="lfChatAddress" maxlength="2000" placeholder="http://IP:8786 или https://адрес"></label><label>Код доступа<input class="text" id="lfChatCode" maxlength="128" placeholder="Код доступа к хосту"></label></div></details><div class="lf-chat-config-actions"><button class="btn" type="submit" id="lfChatApply">Применить вручную</button><span id="lfChatApplyHint">Все комнаты общие; ник — имя для отображения.</span></div></form>
      <p id="lfChatHostHint" class="lf-chat-hint">Для разных сетей нужен доступный внешний адрес с открытым портом или общий VPN. Хост хранит переписку и должен оставаться включённым.</p><p id="lfChatHostInfo" class="lf-chat-hint"></p></details>
      <div class="lf-chat-toolbar"><select id="lfChatRoom" class="select" aria-label="Комната чата"><option value="general">Общий чат</option></select><button class="btn small" id="lfChatNewRoom">＋ Комната</button><span id="lfChatStatus" role="status">Чат выключен</span></div>
      <div class="lf-chat-messages" id="lfChatMessages" role="log" aria-live="polite"></div>
      <form id="lfChatCompose" class="lf-chat-compose"><textarea class="text" id="lfChatMessage" maxlength="2000" rows="2" placeholder="Сообщение…" aria-label="Текст сообщения"></textarea><div class="lf-chat-compose-actions"><button class="btn small" type="button" id="lfChatShareTrack">♫ Советовать трек</button><button class="btn small" type="button" id="lfChatSharePlaylist">☷ Отправить плейлист</button><button class="btn primary" type="submit" id="lfChatSend">Отправить</button></div></form>
      <footer class="lf-chat-foot">При закрытии чата опрос сообщений останавливается. «Выключить чат» также закрывает хост. Прямой HTTP не шифрует переписку: для интернета используй VPN или HTTPS.</footer></div>`;
    document.body.append(panel);
    id("Close").onclick=close;
    panel.addEventListener("click",event=>{if(event.target===panel)close();});
    id("Mode").onchange=syncMode;
    id("InviteAddress").onchange=syncInviteAddress;
    id("JoinInvite").onclick=joinInvitation;
    id("CopyInvite").onclick=copyInvitation;
    id("InviteOutput").onclick=()=>id("InviteOutput").select();
    id("Config").onsubmit=applyConfig;
    id("Enabled").onchange=async()=>{if(!id("Enabled").checked){stop();resetSession();await setSetting({chat_enabled:false},{immediate:true});status("Чат выключен: соединения и хост остановлены");} };
    id("Room").onchange=()=>{stop();state.room=id("Room").value;state.cursor=0;state.messages.clear();renderMessages();poll();};
    id("NewRoom").onclick=newRoom;
    id("Compose").onsubmit=event=>{event.preventDefault();send("text");};
    id("Message").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();send("text");}});
    id("ShareTrack").onclick=shareTrack;
    id("SharePlaylist").onclick=sharePlaylist;
    document.addEventListener("visibilitychange",()=>{if(!state.open)return;if(document.hidden)stop();else poll();});
    document.addEventListener("keydown",event=>{if(event.key==="Escape"&&state.open){event.stopPropagation();close();}});
  }

  function syncMode() {
    const hosting=id("Mode").value==="host";
    id("AddressRow").hidden=hosting;id("HostPanel").hidden=!hosting;id("JoinPanel").hidden=hosting;
    id("ManualTitle").textContent=hosting?"Код доступа (дополнительно)":"Подключиться вручную";
    id("Apply").textContent=hosting?"Создать / сохранить чат":"Применить вручную";
    id("ApplyHint").textContent=hosting?"Включи чат, сохрани настройки, затем скопируй приглашение.":"Для подключения по приглашению достаточно кнопки «Добавить чат».";
    id("HostHint").textContent=hosting?"Хост хранит переписку и должен оставаться включённым. Для разных сетей нужен доступный внешний адрес с пробросом TCP-порта или общий VPN.":"Друг создаёт чат и присылает приглашение. Для разных сетей хосту нужен доступный внешний адрес или общий VPN.";
    syncInviteAddress();
  }

  function syncInviteAddress() {
    const custom=id("InviteAddress").value==="custom";
    id("InviteCustomRow").hidden=!custom;
    id("InviteHint").textContent=custom?"Укажи адрес, доступный другу: общий VPN или внешний адрес с открытым портом. Код сам по себе не открывает доступ через роутер.":"Этот адрес работает в одной локальной сети. Для друга из другой сети выбери внешний / VPN-адрес.";
  }

  function fillConfig() {
    const settings=cfg();id("Enabled").checked=settings.chat_enabled===true;id("Nickname").value=settings.chat_nickname||"";id("Mode").value=settings.chat_host_enabled?"host":"join";id("Port").value=settings.chat_host_port||8786;id("Address").value=settings.chat_server_url||"";id("Code").value=settings.chat_token||"";syncMode();
  }

  async function open() {
    createPanel();state.open=true;panel.classList.remove("hidden");
    id("JoinInvite").disabled=false;id("CopyInvite").disabled=false;fillConfig();
    if(active()){status("Подключаюсь…");await poll();}else status("Введи ник и выбери, создать чат или подключиться к другу");
  }

  function close() {state.open=false;stop();panel.classList.add("hidden");id("InviteOutput").value="";id("InviteOutputRow").hidden=true;id("InviteNotice").textContent="";id("JoinNotice").textContent="";}

  async function joinInvitation() {
    if(id("JoinInvite").disabled)return;
    const nickname=id("Nickname").value.trim(),invite_code=id("InviteInput").value.trim();
    if(nickname.length<2||nickname.length>32){id("JoinNotice").textContent="Введи ник от 2 до 32 символов выше";id("Nickname").focus();return;}
    if(!invite_code){id("JoinNotice").textContent="Вставь код приглашения, который прислал друг";id("InviteInput").focus();return;}
    if(cfg().chat_enabled&&cfg().chat_host_enabled){
      const before=state.generation;id("JoinInvite").disabled=true;
      const accepted=await cutePrompt({title:"Перейти в чат друга?",subtitle:"Твой хост будет выключен, и подключённые к нему друзья потеряют связь с этим чатом.",confirmText:"Перейти в чат друга",confirmOnly:true});
      id("JoinInvite").disabled=false;
      if(!accepted||before!==state.generation||!state.open)return;
    }
    stop();resetSession();state.room="general";renderMessages();
    const generation=state.generation;id("JoinInvite").disabled=true;id("JoinNotice").textContent="Добавляю чат…";
    try {
      const result=await api("/api/chat/invite/join","POST",{invite_code,nickname});
      S.settings={...cfg(),...result.settings};
      if(generation!==state.generation||!state.open)return;
      fillConfig();id("InviteInput").value="";id("JoinNotice").textContent="Чат добавлен";
      if(active())await poll();
    } catch(error) {if(generation===state.generation&&state.open){id("JoinNotice").textContent=error.message;if(active())await poll();}}
    finally {if(generation===state.generation)id("JoinInvite").disabled=false;}
  }

  async function copyInvitation() {
    if(id("CopyInvite").disabled)return;
    if(!cfg().chat_enabled||!cfg().chat_host_enabled){id("InviteNotice").textContent="Сначала включи чат и нажми «Создать / сохранить чат»";return;}
    const url=id("InviteAddress").value==="custom"?id("InviteCustom").value.trim():id("InviteAddress").value;
    if(!url){id("InviteNotice").textContent="Укажи адрес хоста, доступный другу";return;}
    const generation=state.generation;id("CopyInvite").disabled=true;id("InviteNotice").textContent="Создаю приглашение…";
    try {
      const result=await api("/api/chat/invite/create","POST",{url});
      if(generation!==state.generation||!state.open)return;
      id("InviteOutput").value=result.invite_code;id("InviteOutputRow").hidden=false;
      try {
        if(!navigator.clipboard?.writeText)throw new Error("clipboard unavailable");
        await navigator.clipboard.writeText(result.invite_code);
        if(generation===state.generation&&state.open)id("InviteNotice").textContent="Приглашение скопировано. Отправь его другу.";
      } catch(_) {
        if(generation!==state.generation||!state.open)return;
        id("InviteNotice").textContent="Выдели и скопируй код ниже: Ctrl+C. Затем отправь его другу.";
        id("InviteOutput").focus();id("InviteOutput").select();
      }
    } catch(error) {if(generation===state.generation&&state.open)id("InviteNotice").textContent=error.message;}
    finally {if(generation===state.generation)id("CopyInvite").disabled=false;}
  }

  function setHostAddresses(server) {
    if(!server.hosting)return;
    const selected=id("InviteAddress").value,urls=(server.lan_addresses||[]).map(address=>"http://"+address+":"+server.port);
    const options=urls.map(url=>{const option=document.createElement("option");option.value=url;option.textContent=url+" · локальная сеть";return option;});
    const custom=document.createElement("option");custom.value="custom";custom.textContent="Внешний / VPN-адрес…";options.push(custom);id("InviteAddress").replaceChildren(...options);
    id("InviteAddress").value=urls.includes(selected)||selected==="custom"&&id("InviteCustom").value.trim()?selected:(urls[0]||"custom");syncInviteAddress();
  }

  async function applyConfig(event) {
    event.preventDefault();stop();resetSession();const generation=state.generation;
    const enabled=id("Enabled").checked,hosting=id("Mode").value==="host",nickname=id("Nickname").value.trim(),port=Number(id("Port").value);
    if(enabled&&nickname.length<2){status("Введи ник от 2 до 32 символов");return;}
    if(enabled&&hosting&&(!Number.isInteger(port)||port<1024||port>65535)){status("Выбери порт от 1024 до 65535");return;}
    const address=id("Address").value.trim().replace(/\/+$/,"");
    if(enabled&&!hosting){try{const url=new URL(address);if(!["http:","https:"].includes(url.protocol)||url.username||url.password||url.pathname!=="/"||url.search||url.hash)throw new Error();}catch(_){status("Нужен адрес http://IP:порт или https://адрес");return;}}
    let code=id("Code").value.trim();if(enabled&&hosting&&!code){code=uuid().replace(/-/g,"").slice(0,16);id("Code").value=code;}
    status("Сохраняю…");
    const saved=await setSetting({chat_enabled:enabled,chat_host_enabled:hosting,chat_host_port:Number.isInteger(port)?port:8786,chat_nickname:nickname,chat_server_url:address,chat_token:code},{immediate:true});
    if(generation!==state.generation||!state.open)return;
    if(!saved){status("Не удалось сохранить настройки чата");return;}
    if(!enabled){status("Чат выключен: соединения и хост остановлены");return;}
    await poll();
  }

  async function connect() {
    if(state.connecting)return state.connecting;
    const pending=connectSession();state.connecting=pending;
    try{return await pending;}finally{if(state.connecting===pending)state.connecting=null;}
  }

  async function connectSession() {
    const generation=state.generation;
    const settings=cfg(),signature=JSON.stringify([settings.chat_nickname,settings.chat_server_url,settings.chat_host_enabled,settings.chat_host_port,settings.chat_token]);
    if(state.token&&state.signature===signature&&state.expires>Date.now()/1000+30)return;
    resetSession();state.signature=signature;
    const server=await request("/api/chat/status");
    if(generation!==state.generation||!active())return;
    if(server.error)throw new Error(server.error);
    setHostAddresses(server);
    id("HostInfo").textContent=server.hosting?"Хост запущен. Адреса этого компьютера: "+(server.lan_addresses||[]).map(address=>"http://"+address+":"+server.port).join(", ")+". Для друга из другой сети укажи внешний/VPN-адрес.":"";
    const session=await request("/api/chat/session","POST",{nickname:settings.chat_nickname||"",client_id:clientId()});
    if(generation!==state.generation||!active())return;
    state.token=session.token;state.expires=Number(session.expires_at)||0;
    setRooms(session.rooms||[{id:"general",name:"Общий чат"}]);
  }

  function setRooms(rooms) {
    id("Room").replaceChildren(...rooms.map(room=>{const option=document.createElement("option");option.value=room.id;option.textContent=room.name;return option;}));
    if(!rooms.some(room=>room.id===state.room))state.room="general";
    id("Room").value=state.room;
  }

  async function newRoom() {
    if(!active()){status("Сначала подключись к чату");return;}
    const generation=state.generation;
    const name=await cutePrompt({title:"Новая комната",subtitle:"Комната будет видна всем участникам этого хоста",placeholder:"Название комнаты",confirmText:"Создать"});
    if(!name||generation!==state.generation||!active())return;
    try {
      await connect();
      if(generation!==state.generation||!active())return;
      const created=await request("/api/chat/rooms","POST",{name:String(name).trim().slice(0,48)});
      if(generation!==state.generation||!active())return;
      const rooms=await request("/api/chat/rooms");
      if(generation!==state.generation||!active())return;
      stop();state.room=created.room.id;state.cursor=0;state.messages.clear();setRooms(rooms.rooms||[]);renderMessages();poll();
    }catch(error){if(generation===state.generation&&active())status(error.message);}
  }

  async function poll() {
    clearTimeout(state.timer);state.timer=0;if(!active())return;
    const generation=state.generation;
    try {
      await connect();if(generation!==state.generation||!active())return;
      const data=await request("/api/chat/messages?room="+encodeURIComponent(state.room)+"&after="+state.cursor+"&limit=100");
      if(generation!==state.generation||!active())return;
      for(const message of data.items||[])state.messages.set(message.id,message);
      while(state.messages.size>300)state.messages.delete(state.messages.keys().next().value);
      state.cursor=Number(data.cursor)||state.cursor;state.failures=0;renderMessages();status("На связи · "+(cfg().chat_nickname||""));
    } catch(error) {
      if(generation!==state.generation||!active())return;
      if(error.status===401)resetSession();
      state.failures++;status(error.name==="AbortError"?"Соединение не ответило. Повторю позже.":error.message);
    }
    if(generation===state.generation&&active())state.timer=setTimeout(poll,Math.min(60000,state.failures?10000*state.failures:5000));
  }

  function renderMessages() {
    const list=id("Messages"),nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<90;
    const nodes=Array.from(state.messages.values()).map(message=>{
      const article=document.createElement("article");article.className="lf-chat-message";
      const head=document.createElement("div");head.className="lf-chat-message-head";
      const nick=document.createElement("b");nick.textContent=message.nickname;
      const time=document.createElement("time");time.textContent=new Date(message.created_at*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});head.append(nick,time);article.append(head);
      if(message.text){const text=document.createElement("p");text.textContent=message.text;article.append(text);}
      if(message.attachment){const item=message.attachment,card=document.createElement("div");card.className="lf-chat-attachment";const title=document.createElement("b");title.textContent=(message.kind==="playlist"?"☷ ":"♫ ")+item.title;card.append(title);if(item.artist){const artist=document.createElement("span");artist.textContent=item.artist;card.append(artist);}if(item.url){const link=document.createElement("a");link.textContent="Открыть на площадке ↗";const safeLink=safeMusicUrl(item.url);if(safeLink){link.href=safeLink;link.target="_blank";link.rel="noopener noreferrer";card.append(link);}}if(message.kind==="playlist"&&Array.isArray(item.tracks)&&item.tracks.length){const button=document.createElement("button");button.className="btn small";button.textContent="Добавить плейлист · "+item.tracks.length;button.onclick=()=>importPlaylist(item);card.append(button);}article.append(card);}
      return article;
    });
    if(!nodes.length){const empty=document.createElement("p");empty.className="lf-chat-empty";empty.textContent="Здесь пока нет сообщений. Напиши первым или посоветуй трек.";nodes.push(empty);}
    list.replaceChildren(...nodes);if(nearBottom)list.scrollTop=list.scrollHeight;
  }

  function safeMusicUrl(value) {try{const url=new URL(value);return url.protocol==="https:"&&/^(?:.*\.)?(?:youtube\.com|youtu\.be|soundcloud\.com|spotify\.com|apple\.com|music\.yandex\.(?:ru|com|kz))$/i.test(url.hostname)&&!url.username&&!url.password?url.href:"";}catch(_){return "";}}
  function trackAttachment(track) {const url=safeMusicUrl(track?.url);return url?{title:String(track.title||"Без названия").slice(0,180),artist:String(track.artist||"").slice(0,180),url,provider:String(track.provider||"").slice(0,40)}:null;}

  async function send(kind,attachment=null) {
    if(id("Send").disabled)return;
    if(!active()){status("Сначала включи чат и подключись");return;}
    const text=id("Message").value.trim();if(kind==="text"&&!text)return;
    const generation=state.generation;id("Send").disabled=true;
    try {await connect();if(generation!==state.generation||!active())return;const result=await request("/api/chat/messages","POST",{room:state.room,text,kind,attachment,nonce:uuid()});if(generation!==state.generation||!active())return;state.messages.set(result.message.id,result.message);id("Message").value="";renderMessages();await poll();}catch(error){if(generation===state.generation&&active())status(error.message);}finally{id("Send").disabled=false;}
  }

  async function shareTrack() {
    const attachment=trackAttachment(S.current);
    if(!attachment){status("Выбери онлайн-трек с публичной ссылкой. Локальные аудиофайлы в чат не загружаются.");return;}
    await send("track",attachment);
  }

  async function sharePlaylist() {
    if(!active()){status("Сначала подключись к чату");return;}
    const playlists=(S.customPlaylists||[]).map(playlist=>({playlist,tracks:(playlist.ids||[]).map(trackId=>trackAttachment(queuedTrack(trackId))).filter(Boolean)})).filter(item=>item.tracks.length);
    const choice=await cutePrompt({title:"Отправить плейлист",subtitle:"Передаются ссылки на онлайн-треки, до 100 песен. Локальные файлы остаются на компьютере.",options:[{value:"link",label:"Вставить публичную ссылку"},...playlists.map(item=>({value:item.playlist.id,label:item.playlist.name+" · "+item.tracks.length}))],confirmText:"Продолжить"});
    if(!choice)return;
    if(choice==="link"){const url=await cutePrompt({title:"Ссылка плейлиста",subtitle:"Публичная HTTPS-ссылка на музыкальную площадку",placeholder:"https://…",confirmText:"Отправить"});if(!url)return;if(!safeMusicUrl(url)){status("Нужна ссылка на поддерживаемую музыкальную площадку");return;}await send("playlist",{title:"Плейлист",url,tracks:[]});return;}
    const selected=playlists.find(item=>item.playlist.id===choice);if(selected)await send("playlist",{title:selected.playlist.name,url:"",tracks:selected.tracks.slice(0,100)});
  }

  function importPlaylist(attachment) {
    const playlist={id:customPlaylistId(),name:String(attachment.title||"Из чата").slice(0,80),ids:[],remoteTracks:{}};
    for(const raw of attachment.tracks.slice(0,100)){const item=trackAttachment(raw);if(!item)continue;const trackId="chat:"+item.provider+":"+item.url;if(playlist.ids.includes(trackId))continue;const track={...item,id:trackId,remote:true,dur:0,thumbnail:"",stream_url:""};playlist.ids.push(trackId);S.onlineTracks.set(trackId,track);rememberRemoteTrackForPlaylist(playlist,trackId,track);}
    if(!playlist.ids.length){status("В плейлисте нет поддерживаемых ссылок");return;}
    S.customPlaylists.push(playlist);saveCustomPlaylists();renderSidebar();status("Плейлист добавлен: "+playlist.name);
  }

  function bind() {const button=document.getElementById("btnOpenChat");if(button)button.addEventListener("click",open);}
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",bind,{once:true});else bind();
  window.addEventListener("pagehide",stop);
})();
