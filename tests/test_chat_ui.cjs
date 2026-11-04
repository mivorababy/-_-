// Minimal DOM harness checks invitation actions and idle chat lifecycle.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname,"../web/chat.js"),"utf8");
const flush = () => new Promise(resolve=>setImmediate(resolve));
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

function harness({settings={},clipboard,apiImpl,fetchImpl,prompt=async()=>null}={}) {
  const elements=new Map(),timers=new Map(),listeners=new Map(),requests=[],actions=[];
  let timerId=0;
  class Element {
    constructor(tag="div") {this.tagName=tag.toUpperCase();this.children=[];this.events={};this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.scrollHeight=0;this.scrollTop=0;this.clientHeight=100;this.classList={add(){},remove(){},toggle(){}};}
    set innerHTML(value) {this._html=value;for(const match of value.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){const element=new Element(match[1]);element.hidden=/\bhidden\b/.test(match[2]);elements.set(match[3],element);}}
    get innerHTML(){return this._html||"";}
    querySelector(value){return elements.get(value.replace(/^#/,""));}
    setAttribute(){}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=items;}
    addEventListener(name,listener){this.events[name]=listener;}
    focus(){this.focused=true;}
    select(){this.selected=true;}
  }
  const button=new Element("button");elements.set("btnOpenChat",button);
  const document={readyState:"complete",hidden:false,body:new Element("body"),createElement:tag=>new Element(tag),getElementById:name=>elements.get(name),addEventListener:(event,fn)=>listeners.set(event,fn)};
  const context={document,window:{addEventListener(){}},navigator:{clipboard},S:{settings:{chat_enabled:false,...settings}},URL,AbortController,Date,JSON,Array,Map,Set,Math,Number,String,Error,Promise,
    crypto:require("node:crypto").webcrypto,
    localStorage:{getItem:()=>"test-client-0123456789",setItem(){}},tk:pathname=>pathname,
    setSetting:async patch=>{Object.assign(context.S.settings,patch);return true;},cutePrompt:prompt,
    setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
    api:async(pathname,method,body)=>{actions.push({pathname,method,body});if(apiImpl)return apiImpl(pathname,method,body);if(pathname.endsWith("/join"))return {settings:{chat_enabled:true,chat_host_enabled:false,chat_nickname:body.nickname,chat_server_url:"http://10.0.0.2:8786",chat_token:"friend-key"}};return {invite_code:"LFCHAT1.test-invitation",url:body.url};},
    fetch:async(pathname,options)=>{requests.push({pathname,options});let result;if(fetchImpl)result=await fetchImpl(pathname,options);if(!result){if(pathname.startsWith("/api/chat/status"))result={enabled:true,hosting:!!context.S.settings.chat_host_enabled,port:8786,lan_addresses:["192.168.1.5"]};else if(pathname.startsWith("/api/chat/session"))result={token:"session-token",expires_at:Date.now()/1000+3600,rooms:[{id:"general",name:"Общий чат"}]};else result={items:[],cursor:0};}return {ok:true,json:async()=>result};}
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {context,document,timers,listeners,requests,actions,open:()=>button.events.click(),el:name=>elements.get("lfChat"+name),close:()=>elements.get("lfChatClose").onclick()};
}

(async()=>{
  const chat=harness();
  assert.equal(chat.requests.length,0);assert.equal(chat.timers.size,0);
  await chat.open();
  assert.equal(chat.requests.length,0,"opening a disabled chat must not connect");assert.equal(chat.actions.length,0);assert.equal(chat.timers.size,0);
  assert.equal(typeof chat.el("NewRoom").onclick,"function");
  chat.close();Object.assign(chat.context.S.settings,{chat_enabled:true,chat_nickname:"Alice",chat_server_url:"http://127.0.0.1:8786"});await chat.open();
  assert.equal(chat.requests.length,3,"enabled chat requests status, session, messages");assert.equal(chat.timers.size,1);assert.equal([...chat.timers.values()][0].delay,5000);
  chat.document.hidden=true;chat.listeners.get("visibilitychange")();assert.equal(chat.timers.size,0);
  chat.document.hidden=false;chat.listeners.get("visibilitychange")();await flush();assert.equal(chat.timers.size,1);
  chat.close();assert.equal(chat.timers.size,0);
  const prior=chat.requests.length;chat.context.S.settings.chat_enabled=false;await chat.open();assert.equal(chat.requests.length,prior);assert.equal(chat.timers.size,0);

  chat.el("InviteInput").value="LFCHAT1.pasted";chat.el("Nickname").value="";await chat.el("JoinInvite").onclick();
  assert.equal(chat.actions.length,0,"nickname is required before importing");assert.match(chat.el("JoinNotice").textContent,/ник/);
  chat.el("Nickname").value="  Alice  ";await chat.el("JoinInvite").onclick();
  assert.equal(chat.actions.length,1);assert.equal(chat.actions[0].pathname,"/api/chat/invite/join");assert.equal(chat.actions[0].body.nickname,"Alice");assert.equal(chat.actions[0].body.invite_code,"LFCHAT1.pasted");
  assert.equal(chat.context.S.settings.chat_enabled,true);assert.equal(chat.context.S.settings.chat_token,"friend-key");assert.equal(chat.el("Address").value,"http://10.0.0.2:8786");assert.equal(chat.el("InviteInput").value,"");assert.equal(chat.requests.length,prior+3);assert.equal(chat.timers.size,1,"importing while visible starts polling");chat.close();

  const joining=deferred(),late=harness({apiImpl:()=>joining.promise});await late.open();late.el("Nickname").value="Alice";late.el("InviteInput").value="LFCHAT1.delayed";
  const join=late.el("JoinInvite").onclick();late.close();joining.resolve({settings:{chat_enabled:true,chat_nickname:"Alice",chat_host_enabled:false,chat_server_url:"http://10.0.0.2:8786",chat_token:"friend-key"}});await join;
  assert.equal(late.context.S.settings.chat_enabled,true,"completed import keeps local settings in sync with backend");assert.equal(late.requests.length,0,"closing during import must not start a session");assert.equal(late.timers.size,0);assert.equal(late.el("JoinNotice").textContent,"");
  await late.open();assert.equal(late.requests.length,3);assert.equal(late.el("JoinInvite").disabled,false);late.close();

  const invalid=harness({apiImpl:async()=>{throw new Error("Некорректное приглашение");}});await invalid.open();invalid.el("Nickname").value="Alice";invalid.el("InviteInput").value="not-a-code";await invalid.el("JoinInvite").onclick();
  assert.equal(invalid.context.S.settings.chat_enabled,false);assert.equal(invalid.timers.size,0);assert.equal(invalid.requests.length,0);assert.match(invalid.el("JoinNotice").textContent,/Некорректное/);invalid.close();

  const manual=harness();await manual.open();manual.el("Enabled").checked=true;manual.el("Mode").value="join";manual.el("Nickname").value="Manual";manual.el("Address").value="http://10.0.0.3:8786";manual.el("Port").value="8786";manual.el("Code").value="legacy-key";await manual.el("Config").onsubmit({preventDefault(){}});
  assert.equal(manual.context.S.settings.chat_server_url,"http://10.0.0.3:8786");assert.equal(manual.context.S.settings.chat_token,"legacy-key");assert.equal(manual.requests.length,3);manual.close();

  const hosting={chat_enabled:true,chat_host_enabled:true,chat_host_port:8786,chat_nickname:"Alice",chat_token:"host-key"};
  let asked=false;const guarded=harness({settings:hosting,prompt:async options=>{asked=true;assert.equal(options.confirmOnly,true);assert.match(options.subtitle,/друзья потеряют связь/);return null;}});await guarded.open();guarded.el("InviteInput").value="LFCHAT1.other";await guarded.el("JoinInvite").onclick();
  assert.equal(asked,true);assert.equal(guarded.actions.length,0,"switching off a live host requires confirmation");assert.equal(guarded.context.S.settings.chat_host_enabled,true);guarded.close();
  const host=harness({settings:hosting,clipboard:{writeText:async()=>{throw new Error("Clipboard denied");}}});await host.open();
  assert.equal(host.el("InviteAddress").value,"http://192.168.1.5:8786");assert.match(host.el("InviteHint").textContent,/одной локальной сети/);
  host.el("InviteAddress").value="custom";host.el("InviteAddress").onchange();host.el("InviteCustom").value="http://10.8.0.2:8786";await host.el("CopyInvite").onclick();
  assert.equal(host.actions[0].pathname,"/api/chat/invite/create");assert.equal(host.actions[0].body.url,"http://10.8.0.2:8786");assert.equal(host.el("InviteOutputRow").hidden,false);assert.equal(host.el("InviteOutput").value,"LFCHAT1.test-invitation");assert.equal(host.el("InviteOutput").selected,true,"clipboard failure leaves a selectable invitation");assert.match(host.el("InviteNotice").textContent,/Ctrl\+C/);host.close();assert.equal(host.el("InviteOutput").value,"");assert.equal(host.timers.size,0);

  const creating=deferred(),copies=[],closedHost=harness({settings:hosting,apiImpl:()=>creating.promise,clipboard:{writeText:async code=>copies.push(code)}});await closedHost.open();const create=closedHost.el("CopyInvite").onclick();closedHost.close();creating.resolve({invite_code:"LFCHAT1.secret"});await create;
  assert.equal(copies.length,0,"a late invitation must not write the clipboard after closing");assert.equal(closedHost.el("InviteOutput").value,"");assert.equal(closedHost.el("InviteOutputRow").hidden,true);assert.equal(closedHost.timers.size,0);

  const pendingStatus=deferred(),stale=harness({settings:{chat_enabled:true,chat_nickname:"Alice"},fetchImpl:pathname=>pathname==="/api/chat/status"?pendingStatus.promise:undefined});const opening=stale.open();stale.close();pendingStatus.resolve({enabled:true,hosting:false});await opening;
  assert.equal(stale.requests.length,1,"an obsolete status response must not establish a session");assert.equal(stale.timers.size,0);
  console.log("Chat UI OK: disabled import, nickname validation, manual compatibility, invitation fallback, stale actions, and zero idle polling.");
})().catch(error=>{console.error(error);process.exitCode=1;});
