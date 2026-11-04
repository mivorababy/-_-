// Minimal DOM harness checks lifecycle, binding and zero polling when closed/off.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const elements = new Map(), timers = new Map(), listeners = new Map();
let timerId = 0, fetches = 0;
class Element {
  constructor(tag = "div") { this.tagName = tag.toUpperCase();this.children=[];this.events={};this.value="";this.checked=false;this.disabled=false;this.scrollHeight=0;this.scrollTop=0;this.clientHeight=100;this.classList={add(){},remove(){},toggle(){}}; }
  set innerHTML(value) { this._html=value;for(const match of value.matchAll(/id="([^"]+)"/g))elements.set(match[1],new Element()); }
  get innerHTML(){ return this._html||""; }
  querySelector(value){ return elements.get(value.replace(/^#/,"")); }
  setAttribute(){}
  append(...items){ this.children.push(...items); }
  replaceChildren(...items){ this.children=items; }
  addEventListener(name,listener){this.events[name]=listener;}
}
const button = new Element("button");elements.set("btnOpenChat",button);
const settings={chat_enabled:false};
const document={readyState:"complete",hidden:false,body:new Element("body"),createElement:tag=>new Element(tag),getElementById:name=>elements.get(name),addEventListener:(event,fn)=>listeners.set(event,fn)};
const context={document,window:{addEventListener(){}},S:{settings},URL,AbortController,Date,JSON,Array,Map,Set,Math,Number,String,Error,Promise,
  crypto:require("node:crypto").webcrypto,
  localStorage:{getItem:()=>"test-client-0123456789",setItem(){}},
  tk:pathname=>pathname,
  setSetting:async patch=>{Object.assign(settings,patch);return true;},
  cutePrompt:async()=>null,
  setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
  fetch:async pathname=>{fetches++;let result={};if(pathname.startsWith("/api/chat/status"))result={enabled:true,hosting:false};else if(pathname.startsWith("/api/chat/session"))result={token:"session-token",expires_at:Date.now()/1000+3600,rooms:[{id:"general",name:"Общий чат"}]};else if(pathname.startsWith("/api/chat/messages"))result={items:[],cursor:0};return {ok:true,json:async()=>result};}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../web/chat.js"),"utf8"),context);
(async()=>{
  assert.equal(fetches,0);assert.equal(timers.size,0);
  await button.events.click();
  assert.equal(fetches,0,"opening a disabled chat must not connect");assert.equal(timers.size,0);
  assert.equal(typeof elements.get("lfChatNewRoom").onclick,"function");
  elements.get("lfChatClose").onclick();
  Object.assign(settings,{chat_enabled:true,chat_nickname:"Alice",chat_server_url:"http://127.0.0.1:8786"});
  await button.events.click();
  assert.equal(fetches,3,"enabled chat requests status, session, messages");
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,5000);
  document.hidden=true;listeners.get("visibilitychange")();assert.equal(timers.size,0);
  document.hidden=false;await listeners.get("visibilitychange")();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(timers.size,1);
  elements.get("lfChatClose").onclick();assert.equal(timers.size,0);
  const prior=fetches;settings.chat_enabled=false;await button.events.click();
  assert.equal(fetches,prior);assert.equal(timers.size,0);
  console.log("Chat UI lifecycle OK: off/closed/hidden stop timers; enabled connects; room button bound.");
})().catch(error=>{console.error(error);process.exitCode=1;});
