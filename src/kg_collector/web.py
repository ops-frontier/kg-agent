from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .cli import main as collect

OUTPUT_DIR = Path(os.environ.get("KG_OUTPUT_DIR", "/knowledge"))
OWNER = os.environ.get("GH_TARGET_ORGANIZATION", "")
collection_lock = threading.RLock()
collection_condition = threading.Condition(collection_lock)
collection_state: dict[str, Any] = {
    "running": False,
    "message": "待機中",
    "repository": "",
    "completed": 0,
    "total": 0,
}
collection_revision = 0


def update_collection_state(**changes: Any) -> None:
    global collection_revision
    with collection_condition:
        collection_state.update(changes)
        collection_revision += 1
        collection_condition.notify_all()


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader) or {}


def graph_data(repository: str) -> dict[str, Any]:
    root = OUTPUT_DIR / OWNER / repository / "code"
    functions: list[dict[str, Any]] = []
    by_name: dict[str, list[str]] = {}
    by_file_and_name: dict[tuple[str, str], list[str]] = {}
    paths = sorted(root.rglob("*.yaml")) if root.exists() else []
    documents = [(path, read_yaml(path)) for path in paths]
    for path, data in documents:
        file_path = data.get("path", path.name)
        for function in data.get("functions", []):
            name = function.get("name", "<anonymous>")
            node_id = f"{file_path}::{name}"
            item = {"id": node_id, "name": name, "file": file_path, "line": function.get("start_line"), "calls": []}
            functions.append(item)
            by_name.setdefault(name, []).append(node_id)
            by_file_and_name.setdefault((file_path, name), []).append(node_id)
    nodes = {item["id"]: item for item in functions}
    for path, data in documents:
        file_path = data.get("path", path.name)
        for function in data.get("functions", []):
            source_id = f"{file_path}::{function.get('name', '<anonymous>')}"
            if source_id not in nodes:
                continue
            for called_name in function.get("calls", []):
                local_targets = by_file_and_name.get((file_path, called_name), [])
                global_targets = by_name.get(called_name, [])
                if local_targets:
                    targets = local_targets
                elif len(global_targets) == 1:
                    targets = global_targets
                else:
                    targets = [f"external::{called_name}"]
                nodes[source_id]["calls"].extend(dict.fromkeys(targets))
    return {"nodes": list(nodes.values())}


def start_collection() -> bool:
    with collection_condition:
        if collection_state["running"]:
            return False
        update_collection_state(**{
            "running": True,
            "message": "収集中...",
            "repository": "準備中",
            "completed": 0,
            "total": 0,
        })

    def progress(message: str, repository: str) -> None:
        with collection_condition:
            changes: dict[str, Any] = {"message": message, "repository": repository}
            if message == "完了" or message == "失敗":
                changes["completed"] = collection_state["completed"] + 1
            if message == "準備中":
                try:
                    changes["total"] = int(repository.split()[0])
                except (ValueError, IndexError):
                    pass
            update_collection_state(**changes)

    def run() -> None:
        try:
            result = collect([], progress=progress)
            update_collection_state(running=False, message="収集完了" if result == 0 else "収集に失敗しました", repository="")
        except Exception as error:
            update_collection_state(running=False, message=f"収集に失敗しました: {error}", repository="")

    threading.Thread(target=run, name="kg-collection", daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/index":
            self.send_json(read_yaml(OUTPUT_DIR / OWNER / "index.yaml"))
        elif path == "/api/status":
            self.send_status_events()
        elif path.startswith("/api/graph/"):
            self.send_json(graph_data(path.removeprefix("/api/graph/")))
        elif path == "/" or path == "/index.html":
            body = PAGE.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def send_status_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        revision = -1
        try:
            while True:
                with collection_condition:
                    changed = collection_condition.wait_for(
                        lambda: collection_revision != revision,
                        timeout=15,
                    )
                    if changed:
                        revision = collection_revision
                        data = json.dumps(collection_state, ensure_ascii=False)
                message = f"data: {data}\n\n" if changed else ": keep-alive\n\n"
                self.wfile.write(message.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/api/collect":
            self.send_json({"started": start_collection()}, HTTPStatus.ACCEPTED)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web UI listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ナレッジグラフ</title><style>body{margin:0;background:#edf1e8;color:#17221d;font-family:'Noto Sans JP','Yu Gothic',sans-serif}header{height:70px;background:#17221d;color:#f5f4e9;display:flex;align-items:center;justify-content:space-between;padding:0 32px}button{font:inherit;padding:10px 16px;cursor:pointer;border:1px solid #9dceb0;background:#9dceb0;color:#17221d}.layout{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:calc(100vh - 70px)}aside{border-right:1px solid #c8d2c5;padding:28px 18px}nav button{width:100%;text-align:left;margin:3px 0;background:transparent;border:0;border-left:3px solid transparent}nav button.active{border-left-color:#1d604c;background:#dce7dc;color:#1d604c}main{max-width:1100px;padding:42px 5vw;width:100%;box-sizing:border-box}.eyebrow{color:#1d604c;letter-spacing:.14em;font-size:12px}h2{font-size:38px;font-weight:normal;margin:10px 0}.meta{color:#667268}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0}.stat{background:#fbfcf7;border:1px solid #c8d2c5;padding:14px 20px;min-width:130px}.stat b{display:block;font-size:25px;color:#1d604c;font-weight:normal}.panel{background:#fbfcf7;border-top:3px solid #1d604c;padding:22px;margin-top:20px}.repo{display:flex;justify-content:space-between;border-bottom:1px solid #c8d2c5;padding:12px 0}.identifier,.node-file{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.tree{font-size:13px}.tree details{margin:7px 0 7px 17px}.tree>details{margin-left:0}.tree summary{cursor:pointer;padding:5px}.tree summary:hover{background:#e5eee4}.node-file{color:#667268;font-size:11px;margin-left:10px}.empty{color:#667268}@media(max-width:650px){header{padding:0 16px}.layout{display:block}aside{border-right:0;border-bottom:1px solid #c8d2c5;padding:12px}main{padding:28px 18px}h2{font-size:30px}}</style></head><body><header><strong>ナレッジグラフ</strong><button id="collect">ナレッジグラフ収集</button></header><div class="layout"><aside><nav><button class="active" id="dependencies">依存関係探索</button></nav><p id="status">待機中</p></aside><main><div class="eyebrow">KNOWLEDGE GRAPH / EXPLORER</div><h2 id="title">依存関係探索</h2><p class="meta" id="subtitle">リポジトリを選択してください</p><div class="stats" id="stats"></div><section class="panel"><h3>Call Graph</h3><div class="tree" id="tree"><span class="empty">解析済みデータがありません</span></div></section></main></div><script>
const $=id=>document.getElementById(id);
async function get(url){const r=await fetch(url);return r.json()}
function tree(nodes){const map=new Map(nodes.map(n=>[n.id,n]));const incoming=new Set(nodes.flatMap(n=>n.calls));const roots=nodes.filter(n=>!incoming.has(n.id));const render=(n,trail=[])=>{if(trail.includes(n.id))return '<span class="empty">循環参照</span>';const children=n.calls.map(id=>map.get(id)).filter(Boolean);const label=`<b class="identifier">${n.name}</b> <span class="node-file">${n.file}:${n.line||''}</span>`;return children.length?`<details><summary>${label}</summary>${children.map(c=>render(c,[...trail,n.id])).join('')}</details>`:`<details><summary>${label}</summary></details>`};return (roots.length?roots:nodes).map(n=>render(n)).join('')||'<span class="empty">Call Graph がありません</span>'}
async function selectRepo(name,push=true){if(push)history.pushState({},'',`?repository=${encodeURIComponent(name)}`);const index=await get('/api/index');const repo=(index.repositories||[]).find(r=>r.repository.split('/').pop()===name);$('title').classList.add('identifier');$('title').textContent=name;$('subtitle').textContent=repo?.repository||name;const graph=await get('/api/graph/'+encodeURIComponent(name));$('tree').innerHTML=tree(graph.nodes||[]);$('stats').innerHTML=`<div class="stat"><b>${(graph.nodes||[]).length}</b>関数</div><div class="stat"><b>${(graph.nodes||[]).reduce((n,x)=>n+x.calls.length,0)}</b>呼び出し</div>`}
async function showRepositories(push=false){if(push)history.pushState({},'',location.pathname);const index=await get('/api/index');const repos=index.repositories||[];$('title').classList.remove('identifier');$('title').textContent='依存関係探索';$('subtitle').textContent=index.owner?`${index.owner} / ${repos.length} repositories`:'データ未収集';$('tree').innerHTML='';const list=repos.map(r=>`<div class="repo"><span class="identifier">${r.repository}</span><button onclick="selectRepo('${r.repository.split('/').pop()}')">開く</button></div>`).join('');$('stats').innerHTML=list?`<div class="panel" style="width:100%"><h3>リポジトリ</h3>${list}</div>`:'<span class="empty">リポジトリがありません</span>'}
function route(){const name=new URLSearchParams(location.search).get('repository');return name?selectRepo(name,false):showRepositories()}
$('dependencies').onclick=()=>showRepositories(true);window.addEventListener('popstate',route);
const progress=$('status');progress.insertAdjacentHTML('afterend','<p id="progress"></p>');
function updateStatus(s){$('status').textContent=s.message;$('progress').textContent=s.repository?(s.total?`${s.repository} (${s.completed}/${s.total})`:s.repository):'';$('collect').disabled=s.running;if(!s.running&&s.message==='収集完了')route()}
const statusEvents=new EventSource('/api/status');statusEvents.onmessage=event=>updateStatus(JSON.parse(event.data));
$('collect').onclick=async()=>{$('collect').disabled=true;const r=await fetch('/api/collect',{method:'POST'});const result=await r.json();if(!result.started){$('collect').disabled=false;$('status').textContent='すでに収集中です'}};route();
</script></body></html>"""


if __name__ == "__main__":
    main()