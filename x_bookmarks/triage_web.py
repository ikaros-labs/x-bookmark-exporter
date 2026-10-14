"""Tiny local web UI for reviewing a triage plan (stdlib http.server).

The plan file is the only thing it writes. Notes and media are read from the
vault strictly read-only, and only from inside the plan's notes folder.
"""
import ipaddress
import json
import mimetypes
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .triage import (DEFAULT_RULES, DELETE, KEEP, Note, guard_outside, parse_plan, render_plan,
                     topic_folders, validate_folder)
from .storage import atomic_write

MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}


class PlanStore:
    def __init__(self, vault, plan_path):
        self.vault = Path(vault).expanduser().resolve()
        self.plan_path = guard_outside(plan_path, self.vault, "--plan")
        self.lock = threading.Lock()
        entries, context = self.load()
        self.inbox = (self.vault / context["inbox_rel"]).resolve()
        if not self.inbox.is_relative_to(self.vault) or self.inbox == self.vault:
            raise ValueError("Plan inbox path escapes the vault")
        self.root = self.inbox.parent

    def load(self):
        return parse_plan(self.plan_path.read_text(encoding="utf-8"))

    def folders(self, entries):
        existing = list(topic_folders(self.root, self.inbox)) if self.root.is_dir() else []
        # Existing folders first, then the rest by how many notes the plan sends there.
        counts = {}
        for entry in entries:
            counts[entry["action"]] = counts.get(entry["action"], 0) + 1
        others = (set(DEFAULT_RULES) | set(counts)) - set(existing) - {KEEP, DELETE}
        names = existing + sorted(others, key=lambda t: (-counts.get(t, 0), t))
        return names, existing

    def state(self):
        with self.lock:
            entries, context = self.load()
        folders, existing = self.folders(entries)
        out = []
        for entry in entries:
            out.append({k: entry[k] for k in ("file", "action", "checked", "detail")} | {"author": entry.get("author", "")})
        return {"plan": str(self.plan_path), "inbox": context["inbox_rel"], "folders": folders,
                "existing": existing, "entries": out}

    def update(self, file, action=None, checked=None):
        with self.lock:
            entries, context = self.load()
            entry = next((e for e in entries if e["file"] == file), None)
            if entry is None:
                raise KeyError(file)
            if action is not None:
                action = action.strip()
                entry["action"] = action if action in (KEEP, DELETE) else validate_folder(action)
            if checked is not None:
                entry["checked"] = bool(checked)
            context["existing"] = self.folders(entries)[1]
            atomic_write(self.plan_path, render_plan(entries, context).encode())
            return {k: entry[k] for k in ("file", "action", "checked")}

    def note(self, file):
        path = (self.inbox / file).resolve()
        if path.parent != self.inbox or path.suffix != ".md":
            raise KeyError(file)
        if not path.is_file():
            return {"file": file, "missing": True}
        note = Note(path, self.inbox.name)
        media = []
        for ref in note.media:
            ref = urllib.parse.unquote(ref.split("|")[0])
            for base in (self.inbox, self.root):
                candidate = (base / ref).resolve()
                if candidate.is_relative_to(self.root) and candidate.is_file():
                    media.append(candidate.relative_to(self.root).as_posix())
                    break
        return {"file": file, "author": note.author, "author_name": note.meta.get("author_name", ""),
                "created_at": note.meta.get("created_at", ""), "url": note.meta.get("url", ""),
                "title": note.meta.get("article_title", ""), "text": note.text, "media": media,
                "links": [u for u in note.links if urllib.parse.urlsplit(u).scheme in ("http", "https")][:10]}

    def media(self, rel):
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() not in MEDIA_SUFFIXES or not path.is_file():
            raise KeyError(rel)
        return path


def host_allowed(header, server_address):
    """Reject Host headers other than the bound address or loopback (DNS rebinding)."""
    bound_host, port = server_address[:2]
    try:
        parts = urllib.parse.urlsplit("//" + header)
        if not parts.hostname or parts.port != port:
            return False
        if parts.hostname in {"localhost", str(bound_host).lower()}:
            return True
        address = ipaddress.ip_address(parts.hostname)
        return address.is_loopback or ipaddress.ip_address(bound_host).is_unspecified
    except ValueError:
        return False


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def fail(self, error):
            # Details go to the server log only; clients get a generic message.
            print(f"triage-ui: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            self.send(500, {"error": "internal error"})

        def checked_host(self):
            if host_allowed(self.headers.get("Host", ""), self.server.server_address):
                return True
            self.send(421, {"error": "unexpected Host header"})
            return False

        def same_origin_json(self):
            # Browsers always send Origin on fetch POSTs; a missing or foreign one is refused.
            origin = self.headers.get("Origin")
            if origin != f"http://{self.headers.get('Host', '')}":
                return "cross-origin request refused (same-origin Origin header required)"
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return "cross-site request refused"
            if self.headers.get_content_type() != "application/json":
                return "Content-Type must be application/json"
            return None

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.checked_host():
                return
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send(200, PAGE, "text/html; charset=utf-8")
                elif url.path == "/api/plan":
                    self.send(200, store.state())
                elif url.path == "/api/note":
                    self.send(200, store.note(query.get("file", [""])[0]))
                elif url.path == "/media":
                    path = store.media(query.get("path", [""])[0])
                    self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                else:
                    self.send(404, {"error": "not found"})
            except KeyError:
                self.send(404, {"error": "not found"})
            except Exception as error:
                self.fail(error)

        def do_POST(self):
            if not self.checked_host():
                return
            if urllib.parse.urlsplit(self.path).path != "/api/update":
                return self.send(404, {"error": "not found"})
            refused = self.same_origin_json()
            if refused:
                return self.send(403 if "refused" in refused else 415, {"error": refused})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    return self.send(400, {"error": "bad request body size"})
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    return self.send(400, {"error": "invalid JSON"})
                if not isinstance(data, dict) or not isinstance(data.get("file"), str) or \
                        not isinstance(data.get("action", ""), (str, type(None))):
                    return self.send(400, {"error": "expected {file, action?, checked?}"})
                try:
                    self.send(200, store.update(data["file"], data.get("action"), data.get("checked")))
                except ValueError as error:  # folder-name validation; message has no paths
                    self.send(400, {"error": str(error)})
            except KeyError:
                self.send(404, {"error": "unknown entry"})
            except Exception as error:
                self.fail(error)

        def do_PUT(self):
            self.send(405, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT

    return Handler


def serve(vault, plan_path, host="100.66.44.47", port=8724):
    store = PlanStore(vault, plan_path)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"Triage UI for {store.plan_path} on http://{host}:{port}/ (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()




PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Bookmark triage</title>
<style>
:root{--bg:#f6f6f3;--fg:#1d1d1b;--muted:#696963;--line:#e0dfd9;--card:#fff;--sel:#eef3fd;--accent:#2f62c9;--accent-ink:#fff;
 --del:#b4400f;--keep:#5f6570;--warn:#9a6700;--chip:#efeee9;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--fg:#ecebe6;--muted:#a3a29c;--line:#34342f;--card:#1f1f1d;--sel:#1f2a3d;
 --accent:#86aefc;--accent-ink:#0d1526;--del:#fb923c;--keep:#a8adb7;--warn:#e3b341;--chip:#2a2a27;color-scheme:dark}}
*{box-sizing:border-box}[hidden]{display:none!important}html,body{overflow-x:hidden}
body{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
button{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
 padding:calc(10px + env(safe-area-inset-top,0px)) 16px 10px}
.top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h1{font-size:16px;margin:0}#summary{color:var(--muted);flex:1;min-width:0}
#helpbtn{margin-left:auto;flex:none}
.iconbtn{border:1px solid var(--line);background:var(--card);border-radius:999px;width:28px;height:28px;cursor:pointer;font-weight:600}
.chips{display:flex;gap:6px;margin-top:8px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;padding-bottom:2px}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:3px 10px;font-size:12.5px;cursor:pointer;white-space:nowrap}
.chip .n{color:var(--muted);margin-left:4px;font-variant-numeric:tabular-nums}
.chip.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}.chip.on .n{color:inherit;opacity:.8}
.chip kbd{font:600 11px ui-monospace,monospace;color:var(--accent);margin-right:5px}.chip.on kbd{color:inherit}
.chip.delete{color:var(--del)}.chip.keep{color:var(--keep)}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:8px 0 0}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);gap:18px;padding:12px 16px 40px;max-width:1500px;margin:0 auto}
#list{list-style:none;margin:0;padding:0}
#list li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 10px;padding:8px 10px;border-radius:8px;cursor:pointer;border-left:3px solid transparent}
#list li:hover{background:var(--card)}
#list li.sel{background:var(--sel);border-left-color:var(--accent)}
#list li.off .title{color:var(--muted);text-decoration:line-through;text-decoration-color:var(--line)}
#list input{margin:3px 0 0;width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
.title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{grid-column:2;font-size:12px;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{border-radius:999px;padding:0 7px;background:var(--chip);color:var(--fg);font-weight:600;font-size:11.5px}
.pill.delete{color:var(--del)}.pill.keep{color:var(--keep)}.warn{color:var(--warn)}
#detail{position:sticky;top:var(--hdr,140px);align-self:start;background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:16px;max-height:calc(100vh - var(--hdr,140px) - 16px);overflow:auto}
#detail .who{font-size:12.5px;color:var(--muted);margin-bottom:8px}#detail .who b{color:var(--fg)}
#detail h3{margin:0 0 6px;font-size:15px}
#detail .text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}
#detail img,#detail video{display:block;max-width:100%;border-radius:8px;margin-top:10px}
#detail .links{margin:10px 0 0;padding-left:18px;overflow-wrap:anywhere}
.reason{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
.reason div+div{margin-top:3px}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.actions .chip{font-size:13px;padding:5px 11px}
.sheethead{display:none}
#backdrop{display:none}
#err{color:var(--del);font-size:12px}
#help{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;padding:16px}
#help .box{background:var(--card);border-radius:12px;padding:18px 20px;max-width:420px;width:100%;border:1px solid var(--line)}
#help h2{font-size:15px;margin:0 0 10px}#help table{border-collapse:collapse;width:100%}
#help td{padding:4px 0;vertical-align:top}#help td:first-child{width:90px}
kbd.k{font:600 12px ui-monospace,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;background:var(--chip)}
#newfolder{margin-top:8px}#newfolder input{font:inherit;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);width:min(320px,100%)}
@media (max-width:820px){
 main{grid-template-columns:1fr;padding:8px 8px 40px}
 #detail{position:fixed;left:0;right:0;bottom:0;top:auto;z-index:15;max-height:82vh;border-radius:16px 16px 0 0;
  padding:12px 16px calc(16px + env(safe-area-inset-bottom,0px));transform:translateY(105%);transition:transform .2s ease;box-shadow:0 -8px 30px rgba(0,0,0,.2)}
 body.sheet #detail{transform:none}
 body.sheet #backdrop{display:block;position:fixed;inset:0;z-index:14;background:rgba(0,0,0,.3)}
 .sheethead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
 .sheethead .grab{width:40px;height:4px;border-radius:2px;background:var(--line);margin:0 auto}
 .hidemobile{display:none}
 #summary{order:3;flex-basis:100%;font-size:13px}
}
</style></head><body>
<header id="hdr">
 <div class="top"><h1>Bookmark triage</h1><span id="summary">Loading…</span>
  <button class="iconbtn" id="helpbtn" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">?</button></div>
 <div class="chips" id="filters" aria-label="Filter"></div>
 <div class="label hidemobile">File selected note into</div>
 <div class="chips hidemobile" id="keys"></div>
 <form id="newfolder" hidden><input id="nf" placeholder="New folder name, then Enter" autocomplete="off"></form>
 <div id="err" role="alert"></div>
</header>
<main><ul id="list" aria-label="Notes"></ul><section id="detail" aria-live="polite">Loading…</section></main>
<div id="backdrop"></div>
<div id="help" hidden><div class="box"><h2>Keyboard shortcuts</h2><table>
 <tr><td><kbd class="k">j</kbd> <kbd class="k">k</kbd></td><td>Next / previous note</td></tr>
 <tr><td><kbd class="k">1</kbd>–<kbd class="k">0</kbd></td><td>File into that folder (shown on the chips)</td></tr>
 <tr><td><kbd class="k">d</kbd></td><td>Mark for delete</td></tr>
 <tr><td><kbd class="k">i</kbd></td><td>Keep in inbox</td></tr>
 <tr><td><kbd class="k">x</kbd></td><td>Tick / untick (unticked entries are skipped on apply)</td></tr>
 <tr><td><kbd class="k">n</kbd></td><td>File into a new folder</td></tr>
 <tr><td><kbd class="k">o</kbd></td><td>Open the post on X</td></tr>
 <tr><td><kbd class="k">?</kbd> <kbd class="k">Esc</kbd></td><td>Show / hide this help</td></tr>
</table><p style="color:var(--muted);font-size:12.5px;margin:12px 0 0">Every change is saved to the plan file immediately.</p></div></div>
<script>
const $=s=>document.querySelector(s);let S={entries:[],folders:[],existing:[]},sel=0,view=[],hot={},filter="";
const mobile=matchMedia("(max-width:820px)");
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function parts(e){const m=e.file.match(/^(\d{4}-\d{2}-\d{2}|undated) - (.+?) - (.*) - (\d+)\.md$/);
 return m?{date:m[1],author:e.author||m[2],title:m[3]}:{date:"",author:e.author||"",title:e.file.replace(/\.md$/,"")};}
const flagged=e=>e.detail.includes("⚠");
async function load(){const r=await fetch("/api/plan");S=await r.json();render();}
function counts(){const c={};S.entries.forEach(e=>c[e.action]=(c[e.action]||0)+1);return c;}
function header(){const c=counts(),n=S.entries.length,del=S.entries.filter(e=>e.action==="delete"),
 fil=S.entries.filter(e=>e.action!=="delete"&&e.action!=="keep"&&e.checked).length,
 skip=S.entries.filter(e=>!e.checked&&e.action!=="keep"&&e.action!=="delete").length,cand=del.filter(e=>!e.checked).length;
 $("#summary").textContent=`${n} notes: ${fil} to file, ${del.length-cand} to delete`+(cand?` (+${cand} candidates to confirm)`:"")+`, ${c.keep||0} stay`+(skip?`, ${skip} skipped`:"");
 const acts=Object.entries(c).sort((a,b)=>(a[0]==="keep"||a[0]==="delete")-(b[0]==="keep"||b[0]==="delete")||b[1]-a[1]);
 const fl=S.entries.filter(flagged).length;
 $("#filters").innerHTML=[["","All",n],...acts.map(([a,k])=>[a,a,k]),["__flag","⚠ flagged",fl]]
  .map(([v,t,k])=>`<button class="chip ${esc(v)} ${filter===v?"on":""}" data-f="${esc(v)}">${esc(t)}<span class="n">${k}</span></button>`).join("");
 hot={};const ks="1234567890";S.folders.slice(0,10).forEach((f,i)=>hot[ks[i]]=f);
 $("#keys").innerHTML=Object.entries(hot).sort((a,b)=>ks.indexOf(a[0])-ks.indexOf(b[0])).map(([k,f])=>
  `<button class="chip" data-a="${esc(f)}" title="${S.existing.includes(f)?"existing folder":"new folder"}"><kbd>${k}</kbd>${esc(f)}${S.existing.includes(f)?"":" <span class=n>new</span>"}</button>`).join("")
  +`<button class="chip delete" data-a="delete"><kbd>d</kbd>delete</button><button class="chip keep" data-a="keep"><kbd>i</kbd>keep</button><button class="chip" data-new="1"><kbd>n</kbd>new folder…</button>`;
 document.documentElement.style.setProperty("--hdr",($("#hdr").offsetHeight+12)+"px");}
function render(){view=S.entries.filter(e=>!filter||(filter==="__flag"?flagged(e):e.action===filter));
 if(sel>=view.length)sel=Math.max(0,view.length-1);header();
 const one=filter&&filter!=="__flag";
 $("#list").innerHTML=view.length?view.map((e,i)=>{const p=parts(e);return `<li data-i="${i}" class="${i===sel?"sel":""} ${e.checked?"":"off"}" aria-selected="${i===sel}">
  <input type="checkbox" ${e.checked?"checked":""} aria-label="Include in apply" data-c="${i}">
  <span class="title" title="${esc(e.file)}">${esc(p.title||"(untitled)")}</span>
  <span class="meta">${one?"":`<span class="pill ${esc(e.action)}">${esc(e.action)}</span>`}<span>@${esc(p.author)}</span><span>${esc(p.date)}</span>${flagged(e)?'<span class="warn" title="flagged">⚠</span>':""}</span></li>`}).join("")
  :'<li style="color:var(--muted);cursor:default">Nothing here.</li>';
 const li=$("#list li.sel");if(li&&!mobile.matches)li.scrollIntoView({block:"nearest"});detail();}
let detailFor=null,noteCache={};
function reason(e){const segs=e.detail.split(" · "),why=segs.filter(s=>s.startsWith("why: ")).map(s=>s.slice(5)),
 warn=segs.filter(s=>s.startsWith("⚠ ")).map(s=>s.slice(2));
 return (why.length?`<div>Why <b>${esc(e.action)}</b>: ${esc(why.join(", "))}</div>`:`<div>Proposed: <b>${esc(e.action)}</b></div>`)+warn.map(w=>`<div class="warn">⚠ ${esc(w)}</div>`).join("");}
function actions(e){return `<div class="actions">`+S.folders.map(f=>`<button class="chip ${e.action===f?"on":""}" data-a="${esc(f)}">${esc(f)}</button>`).join("")+
 `<button class="chip delete ${e.action==="delete"?"on":""}" data-a="delete">delete</button><button class="chip keep ${e.action==="keep"?"on":""}" data-a="keep">keep</button>`+
 `<button class="chip" data-new="1">new folder…</button><button class="chip" data-t="1">${e.checked?"☑ ticked":"☐ unticked"}</button></div>`;}
async function detail(){const e=view[sel],d=$("#detail");if(!e){d.innerHTML='<span style="color:var(--muted)">Nothing selected.</span>';return;}
 detailFor=e.file;const n=noteCache[e.file]||(noteCache[e.file]=await (await fetch("/api/note?file="+encodeURIComponent(e.file))).json());
 if(detailFor!==e.file)return;const p=parts(e);
 const media=(n.media||[]).map(m=>/\.mp4$/i.test(m)?`<video controls preload="none" src="/media?path=${encodeURIComponent(m)}"></video>`:`<img loading="lazy" alt="" src="/media?path=${encodeURIComponent(m)}">`).join("");
 const links=(n.links||[]).length?`<ul class="links">${n.links.map(u=>`<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u.replace(/^https?:\/\/(www\.)?/,""))}</a></li>`).join("")}</ul>`:"";
 d.innerHTML=`<div class="sheethead"><span></span><span class="grab"></span><button class="iconbtn" id="close" aria-label="Close">×</button></div>
  <div class="who"><b>@${esc(n.author||p.author)}</b> ${esc(n.author_name)} · ${esc((n.created_at||p.date).slice(0,10))}${n.url?` · <a href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">open on X</a>`:""}</div>
  ${n.missing?"<p><i>This note is no longer in the inbox.</i></p>":""}${n.title?`<h3>${esc(n.title)}</h3>`:""}
  <div class="text">${esc(n.text||"(no text)")}</div>${media}${links}<div class="reason">${reason(e)}</div>${actions(e)}`;}
async function update(e,patch){$("#err").textContent="";const r=await fetch("/api/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:e.file,...patch})});
 const j=await r.json();if(!r.ok){$("#err").textContent=j.error||"Update failed";return false;}Object.assign(e,j);return true;}
async function assign(a,advance=true){const e=view[sel];if(!e)return;if(await update(e,{action:a,checked:true})){
 if(!S.folders.includes(a)&&a!=="keep"&&a!=="delete")S.folders.push(a);
 if(advance&&!mobile.matches)sel=Math.min(sel+1,view.length-1);render();}}
async function toggle(e){if(e&&await update(e,{checked:!e.checked}))render();}
function openSheet(){if(mobile.matches)document.body.classList.add("sheet");}
function closeSheet(){document.body.classList.remove("sheet");}
function askFolder(){$("#newfolder").hidden=false;$("#nf").focus();}
document.addEventListener("keydown",async ev=>{if(ev.target.tagName==="INPUT"&&ev.target.type!=="checkbox"||ev.metaKey||ev.ctrlKey||ev.altKey)return;const k=ev.key,e=view[sel];
 if(k==="?"){$("#help").hidden=!$("#help").hidden;}
 else if(k==="Escape"){$("#help").hidden=true;closeSheet();}
 else if(k==="j"||k==="ArrowDown"){sel=Math.min(sel+1,view.length-1);render();}
 else if(k==="k"||k==="ArrowUp"){sel=Math.max(sel-1,0);render();}
 else if(hot[k])assign(hot[k]);else if(k==="d")assign("delete");else if(k==="i")assign("keep");
 else if(k==="x"||k===" ")toggle(e);else if(k==="n")askFolder();
 else if(k==="o"&&e){const n=noteCache[e.file];if(n&&n.url)window.open(n.url,"_blank","noopener");}
 else return;ev.preventDefault();});
$("#newfolder").addEventListener("submit",ev=>{ev.preventDefault();const v=$("#nf").value.trim();$("#nf").value="";$("#newfolder").hidden=true;$("#nf").blur();if(v)assign(v);});
$("#nf").addEventListener("keydown",ev=>{if(ev.key==="Escape"){$("#newfolder").hidden=true;$("#nf").blur();}});
$("#list").addEventListener("click",ev=>{const li=ev.target.closest("li[data-i]");if(!li)return;sel=+li.dataset.i;
 if(ev.target.dataset.c!==undefined){ev.preventDefault();toggle(view[sel]);return;}render();openSheet();});
document.addEventListener("click",ev=>{const b=ev.target.closest("button");if(!b)return;
 if(b.dataset.f!==undefined){filter=b.dataset.f;sel=0;closeSheet();render();}
 else if(b.dataset.a){assign(b.dataset.a,!b.closest("#detail"));}
 else if(b.dataset.new){askFolder();}else if(b.dataset.t){toggle(view[sel]);}
 else if(b.id==="close")closeSheet();else if(b.id==="helpbtn")$("#help").hidden=false;});
$("#backdrop").addEventListener("click",closeSheet);
$("#help").addEventListener("click",ev=>{if(ev.target.id==="help")$("#help").hidden=true;});
addEventListener("resize",()=>document.documentElement.style.setProperty("--hdr",($("#hdr").offsetHeight+12)+"px"));
load();
</script></body></html>
"""
