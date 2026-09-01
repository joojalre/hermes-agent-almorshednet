from __future__ import annotations
import argparse, ast, json, re, shutil, subprocess, textwrap
from pathlib import Path

RELEASE="29112bef099274229cadff79cdff7bf7b99c4b77"
PRELUDE="ca18e49eaeda7523fef201d756b8e081ec1bddb3"
FIRST="3b323b078967ddb29ca0f792f007ccc6a559e79e"
SECOND="8d42059049e155c1e93f34505e63d3995cedb5b9"
REST="""18d6a8ab71bc77183717d424bd83ee356808d8c0
b306ba41e62057d4f5d51c70493fdad0c8ae75df
60767cb87f3c532f003adc6448ec7aa6e1e925d4
28118f12865779ba02eb70d108bb49580322544a
87dec320debe42f6288d89327ee5d84669d8a2cf
36ac9d4be0a9222b20eba30e4f973d0d21eab4d3
b1ea144797a10c440b166d7b3cb79e24817b8173
327696f2770fcbcf711c954ec97533525bf98c76
e3fd97d9781aec7efc6ce6cd5d291dc81b024bd1
acf3708e2475ca5f868bfc7326c5f76289177bb0
5af994bf71b710955ea4710a2198067b253ab3b6
e7d3e23562ee996a25c5e6ff025ef0fde90fe6a5
d3b9806383d10ee063333f39f1b8c4023ad9a685
e5780d2e9c9e40a14214fc2712e750aec6989c9e
37be945a99611cf3b5589746726e376b8164bcb6
d1ef0ea7d033e915ed1089c4612edbfdc4f445e1
c1875cad92ff6b005cb2523ee1bc89b116b5c2cd
4431c43453e8586ca1a8a42fae5b339d661fa126
27cbc9bbbb9ade9c9fe13d6549a2acf6b8292993
10618eb0979f4c3af980f8ce380d9c0fb06fc5ca
7c6a0c8e648b441bf89ecdef5e53c8410cd0d174
6db80964f7c25c99e577f6d550ad0568f833a5dd
b7f1acdef0308ac75f09c3aa41702686e420b599
20d0a6a42365b2b2351e1dca819022b6ec477b39""".split()
FILES=["gateway/hosted_rooms.py","tests/gateway/test_hosted_rooms.py","tests/tui_gateway/test_hosted_room_driver_runtime.py","tests/tui_gateway/test_hosted_room_service.py","tui_gateway/hosted_room_driver.py","tui_gateway/hosted_room_service.py"]

def g(repo,*args,check=True):
    p=subprocess.run(["git","-C",str(repo),*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and p.returncode: raise RuntimeError(f"git {' '.join(args)} ({p.returncode})\n{p.stdout}\n{p.stderr}")
    return p
def out(repo,*args): return g(repo,*args).stdout.strip()
def commit(repo,src):
    g(repo,"add","-A"); g(repo,"commit","--no-gpg-sign","--no-verify","-C",src); return out(repo,"rev-parse","HEAD")
def conflict(repo,src):
    p=g(repo,"cherry-pick","--no-commit",src,check=False)
    if p.returncode==0: raise RuntimeError(f"{src}: expected conflict")

def resolve(root,path,fn):
    p=root/path; lines=p.read_text(encoding="utf-8").splitlines(keepends=True); r=[]; i=n=0
    while i<len(lines):
        if not lines[i].startswith("<<<<<<< "): r.append(lines[i]); i+=1; continue
        n+=1; i+=1; ours=[]
        while i<len(lines) and not lines[i].startswith("||||||| "): ours.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError(f"{path}: no base")
        i+=1; base=[]
        while i<len(lines) and not lines[i].startswith("======="): base.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError(f"{path}: no separator")
        i+=1; theirs=[]
        while i<len(lines) and not lines[i].startswith(">>>>>>> "): theirs.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError(f"{path}: no end")
        i+=1; x=fn(n,"".join(ours),"".join(base),"".join(theirs))
        r.append(x if not x or x.endswith("\n") else x+"\n")
    text="".join(r)
    if any(x in text for x in ("<<<<<<<","|||||||",">>>>>>>")): raise RuntimeError(f"{path}: marker")
    p.write_text(text,encoding="utf-8")

def first(repo):
    resolve(repo,"gateway/hosted_rooms.py",lambda n,o,b,t:t)
    resolve(repo,"tests/gateway/test_hosted_rooms.py",lambda n,o,b,t:o.rstrip()+"\n\n"+t.lstrip())
    def runtime_test(n,o,b,t):
        if n==1:
            ind=re.match(r"(\s*)",o).group(1); return f"{ind}rpc.history_failures = 1\n{ind}now[0] = 102.0\n"
        if n in (2,3): return o
        raise RuntimeError(n)
    resolve(repo,"tests/tui_gateway/test_hosted_room_driver_runtime.py",runtime_test)
    def service_test(n,o,b,t):
        if n==1:return "import hashlib\nimport multiprocessing\n"
        if n==2:return t.rstrip()+"\n\n"+o.lstrip()
        raise RuntimeError(n)
    resolve(repo,"tests/tui_gateway/test_hosted_room_service.py",service_test)
    def driver(n,o,b,t):
        if n!=1: raise RuntimeError(n)
        code=textwrap.dedent("""\
receipt = _durable_terminal_receipt(
    state.get_terminal_receipt(
        self.db_path,
        task["identity"],
        execution_generation=int(task["execution_generation"]),
    )
)
if receipt is None:
    transport = self._transport_for(binding, task)
    if transport is None:
        return False
    profile = task["payload"]["target_profile"]
    session = transport.resolve_exact(
        profile=profile,
        title=room_session_title(binding.room_id),
        source=ROOM_SESSION_SOURCE,
    )
    if session is None:
        return False
    resumed = transport.resume(
        profile=profile,
        session_id=_session_id(session),
        source=ROOM_SESSION_SOURCE,
    )
    receipt = _find_terminal_receipt(
        transport.history(
            profile=profile,
            session_id=_session_id(resumed),
            source=ROOM_SESSION_SOURCE,
        ),
        task["identity"],
        int(task["execution_generation"]),
""")
        return textwrap.indent(code,"        ")
    resolve(repo,"tui_gateway/hosted_room_driver.py",driver)
    resolve(repo,"tui_gateway/hosted_room_service.py",lambda n,o,b,t:o)
    p=repo/"tui_gateway/hosted_room_service.py"; s=p.read_text(encoding="utf-8")
    a=s.index("    def approve_room_task("); z=s.index("    def status(",a)
    code=textwrap.dedent("""\
def approve_room_task(
    self, room_id: str, *, member_id: str, task_id: str,
    execution_generation: int, choice: str, request_id: str | None = None,
) -> Mapping[str, Any]:
    \"\"\"Resolve one exact local or peer approval and wake observation.\"\"\"
    requested_approval_id = str(request_id or "")
    if not requested_approval_id:
        raise RuntimeError("room approval is no longer pending")
    if choice not in {"once", "deny"}:
        raise RuntimeError("room approval choice must be once or deny")
    key = (room_id, member_id)
    route = self.peer_routes.get(key)
    client = self.peer_clients.get(key)
    if route is not None:
        with self._policy_lock:
            action = self._pending_actions.get(key)
        pending_approval_id = str((action or {}).get("request_id") or "")
        if (
            action is None or action.get("task_id") != task_id
            or int(action.get("execution_generation") or 0) != execution_generation
            or requested_approval_id != pending_approval_id
        ):
            raise RuntimeError("room approval is no longer pending")
        approve = getattr(client, "approve_receipt", None)
        if not callable(approve):
            raise RuntimeError("room approval target is unavailable")
        result = approve(
            task_id=task_id, execution_generation=execution_generation,
            request_id=requested_approval_id, choice=choice, grant=route.grant,
        )
        if result is None:
            raise RuntimeError("room approval target is unavailable")
        with self._policy_lock:
            current = self._pending_actions.get(key)
            if (
                current is not None
                and str(current.get("request_id") or "") == requested_approval_id
                and current.get("task_id") == task_id
                and int(current.get("execution_generation") or 0) == execution_generation
            ):
                self._pending_actions.pop(key, None)
    else:
        pending = next(
            (
                request for request in driver.list_pending_approval_requests(
                    self.db_path, room_id=room_id,
                )
                if request["identity"].task_id == task_id
                and request["execution_generation"] == execution_generation
                and request["member_id"] == member_id
                and request["request_id"] == requested_approval_id
            ),
            None,
        )
        if pending is None:
            raise RuntimeError("room approval is no longer pending")
        result = driver.decide_approval_request(
            self.db_path, pending["identity"],
            execution_generation=execution_generation, member_id=member_id,
            request_id=requested_approval_id, choice=choice, clock=time.time,
        )
    self.runtime.wakeup()
    return result

""")
    p.write_text(s[:a]+textwrap.indent(code,"    ")+s[z:],encoding="utf-8")

def verify(paths):
    for p in paths:
        if p.is_file() and p.suffix==".py": ast.parse(p.read_text(encoding="utf-8"),filename=str(p))

def capture(repo,report,src):
    d=report/"next-conflict"; (d/"files").mkdir(parents=True,exist_ok=True)
    paths=[x for x in out(repo,"diff","--name-only","--diff-filter=U").splitlines() if x]
    (d/"commit.txt").write_text(src+"\n"); (d/"files.txt").write_text("".join(x+"\n" for x in paths))
    (d/"status.txt").write_text(out(repo,"status","--short")+"\n")
    for path in paths:
        q=d/"files"/path; q.mkdir(parents=True,exist_ok=True)
        for stage,name in (("1","base"),("2","current"),("3","incoming")):
            p=g(repo,"show",f":{stage}:{path}",check=False); (q/name).write_text(p.stdout or "")
        for a,b,name in (("base","current","base-to-current.diff"),("base","incoming","base-to-incoming.diff"),("current","incoming","current-to-incoming.diff")):
            p=subprocess.run(["git","diff","--no-index","--histogram","--",str(q/a),str(q/b)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            (q/name).write_text(p.stdout or "")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--repo",required=True); ap.add_argument("--report",required=True); a=ap.parse_args()
    repo=Path(a.repo).resolve(); report=Path(a.report).resolve(); report.mkdir(parents=True,exist_ok=True); (report/"resolved-stage1").mkdir(exist_ok=True)
    if out(repo,"rev-parse","HEAD")!=RELEASE or out(repo,"status","--porcelain"): raise RuntimeError("bad candidate start")
    g(repo,"config","user.name","Hermes v0.21.0 Reconciler"); g(repo,"config","user.email","v021-reconciler@invalid.local"); g(repo,"config","merge.conflictStyle","diff3")
    g(repo,"fetch","--no-tags","origin","refs/pull/19/head:refs/remotes/origin/pr19-head")
    applied=[]; empty=[]
    g(repo,"cherry-pick","--no-commit",PRELUDE); applied.append((PRELUDE,commit(repo,PRELUDE)))
    conflict(repo,FIRST); first(repo); g(repo,"add",*FILES)
    if out(repo,"diff","--name-only","--diff-filter=U"): raise RuntimeError("first unresolved")
    verify([repo/x for x in FILES])
    for x in FILES:
        q=report/"resolved-stage1"/x; q.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(repo/x,q)
    applied.append((FIRST,commit(repo,FIRST)))
    conflict(repo,SECOND); path="tests/tui_gateway/test_hosted_room_driver_runtime.py"; g(repo,"checkout","--ours","--",path); g(repo,"add",path)
    if out(repo,"diff","--name-only","--diff-filter=U"): raise RuntimeError("second unresolved")
    verify([repo/path]); applied.append((SECOND,commit(repo,SECOND)))
    failed=""
    for src in REST:
        parents=len(out(repo,"rev-list","--parents","-n1",src).split())-1; cmd=["cherry-pick","--no-commit"]+(["-m","1"] if parents>1 else [])+[src]
        p=g(repo,*cmd,check=False)
        if p.returncode:
            failed=src; capture(repo,report,src); g(repo,"cherry-pick","--abort",check=False); g(repo,"reset","--hard","HEAD"); break
        if not out(repo,"diff","--cached","--name-only") and not out(repo,"diff","--name-only"): empty.append(src); continue
        applied.append((src,commit(repo,src)))
    if out(repo,"status","--porcelain"): raise RuntimeError("dirty candidate")
    g(repo,"diff",f"{RELEASE}..HEAD","--check")
    py=[repo/x for x in out(repo,"diff","--name-only",f"{RELEASE}..HEAD","--","*.py").splitlines() if x and (repo/x).is_file()]; verify(py)
    project=next((m.group(1) for line in (repo/"pyproject.toml").read_text().splitlines() if (m:=re.fullmatch(r'version = "([^"]+)"',line))),"")
    m=re.search(r'^__version__ = "([^"]+)"',(repo/"hermes_cli/__init__.py").read_text(),re.M); cli=m.group(1) if m else ""
    if (project,cli)!=("0.21.0","0.21.0"): raise RuntimeError(f"versions {project}/{cli}")
    (report/"applied.tsv").write_text("".join(f"{a}\t{b}\n" for a,b in applied)); (report/"empty.txt").write_text("".join(x+"\n" for x in empty))
    summary={"release":RELEASE,"candidate_head":out(repo,"rev-parse","HEAD"),"candidate_tree":out(repo,"rev-parse","HEAD^{tree}"),"applied_count":len(applied),"empty_count":len(empty),"failed_commit":failed,"project_version":project,"cli_version":cli,"changed_python_compiled":len(py)}
    (report/"summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n"); print(json.dumps(summary,sort_keys=True))

if __name__=="__main__": main()
