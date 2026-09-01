from __future__ import annotations
import argparse, ast, json, re, subprocess
from pathlib import Path

START="716feb04a36194a7cdce50d7cec9f3ed06a9429f"
FIRST="18d6a8ab71bc77183717d424bd83ee356808d8c0"
REST="""b306ba41e62057d4f5d51c70493fdad0c8ae75df
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
RELEASE="29112bef099274229cadff79cdff7bf7b99c4b77"

def g(repo,*args,check=True):
    p=subprocess.run(["git","-C",str(repo),*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and p.returncode: raise RuntimeError(f"git {' '.join(args)} ({p.returncode})\n{p.stdout}\n{p.stderr}")
    return p
def out(repo,*args): return g(repo,*args).stdout.strip()
def commit(repo,src): g(repo,"add","-A"); g(repo,"commit","--no-gpg-sign","--no-verify","-C",src); return out(repo,"rev-parse","HEAD")
def conflict(repo,src):
    p=g(repo,"cherry-pick","--no-commit",src,check=False)
    if p.returncode==0: raise RuntimeError(f"{src}: expected conflict")

def resolve(path,fn):
    lines=path.read_text(encoding="utf-8").splitlines(keepends=True); r=[]; i=n=0
    while i<len(lines):
        if not lines[i].startswith("<<<<<<< "): r.append(lines[i]); i+=1; continue
        n+=1; i+=1; ours=[]
        while i<len(lines) and not lines[i].startswith("||||||| "): ours.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError("no base")
        i+=1; base=[]
        while i<len(lines) and not lines[i].startswith("======="): base.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError("no separator")
        i+=1; theirs=[]
        while i<len(lines) and not lines[i].startswith(">>>>>>> "): theirs.append(lines[i]); i+=1
        if i>=len(lines): raise RuntimeError("no end")
        i+=1; x=fn(n,"".join(ours),"".join(base),"".join(theirs)); r.append(x if x.endswith("\n") else x+"\n")
    text="".join(r)
    if any(x in text for x in ("<<<<<<<","|||||||",">>>>>>>")): raise RuntimeError("marker")
    path.write_text(text,encoding="utf-8")

def capture(repo,report,src):
    d=report/"next-conflict"; (d/"files").mkdir(parents=True,exist_ok=True)
    paths=[x for x in out(repo,"diff","--name-only","--diff-filter=U").splitlines() if x]
    (d/"commit.txt").write_text(src+"\n"); (d/"files.txt").write_text("".join(x+"\n" for x in paths)); (d/"status.txt").write_text(out(repo,"status","--short")+"\n")
    for path in paths:
        q=d/"files"/path; q.mkdir(parents=True,exist_ok=True)
        for stage,name in (("1","base"),("2","current"),("3","incoming")):
            p=g(repo,"show",f":{stage}:{path}",check=False); (q/name).write_text(p.stdout or "")
        for a,b,name in (("base","current","base-to-current.diff"),("base","incoming","base-to-incoming.diff"),("current","incoming","current-to-incoming.diff")):
            p=subprocess.run(["git","diff","--no-index","--histogram","--",str(q/a),str(q/b)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            (q/name).write_text(p.stdout or "")

def verify(repo):
    g(repo,"diff",f"{RELEASE}..HEAD","--check")
    py=[repo/x for x in out(repo,"diff","--name-only",f"{RELEASE}..HEAD","--","*.py").splitlines() if x and (repo/x).is_file()]
    for p in py: ast.parse(p.read_text(encoding="utf-8"),filename=str(p))
    return len(py)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--repo",required=True); ap.add_argument("--report",required=True); a=ap.parse_args()
    repo=Path(a.repo).resolve(); report=Path(a.report).resolve(); report.mkdir(parents=True,exist_ok=True)
    if out(repo,"rev-parse","HEAD")!=START or out(repo,"status","--porcelain"): raise RuntimeError("bad start")
    g(repo,"config","user.name","Hermes v0.21.0 Reconciler"); g(repo,"config","user.email","v021-reconciler@invalid.local"); g(repo,"config","merge.conflictStyle","diff3")
    g(repo,"fetch","--no-tags","origin","refs/pull/19/head:refs/remotes/origin/pr19-head")
    applied=[]; empty=[]
    conflict(repo,FIRST)
    path=repo/"tests/tools/test_bot_mode_dm.py"
    def choose(n,o,b,t):
        if n==1:return "            message=message,\n"
        if n==2:return "    assert message not in command\n"
        raise RuntimeError(n)
    resolve(path,choose)
    text=path.read_text(encoding="utf-8")
    old='    message = \'status? give me the "final" numbers $(and this is not shell)\'\n'
    new='    message = (\n        \'status? give me the "PAYLOAD_SENTINEL_7A91" numbers \'\n        "$(and this is not shell)"\n    )\n'
    if old not in text: raise RuntimeError("message fixture changed")
    path.write_text(text.replace(old,new,1),encoding="utf-8")
    g(repo,"add","tests/tools/test_bot_mode_dm.py")
    if out(repo,"diff","--name-only","--diff-filter=U"): raise RuntimeError("first unresolved")
    ast.parse(path.read_text(encoding="utf-8"),filename=str(path)); applied.append((FIRST,commit(repo,FIRST)))
    failed=""
    for src in REST:
        parents=len(out(repo,"rev-list","--parents","-n1",src).split())-1; cmd=["cherry-pick","--no-commit"]+(["-m","1"] if parents>1 else [])+[src]
        p=g(repo,*cmd,check=False)
        if p.returncode:
            failed=src; capture(repo,report,src); g(repo,"cherry-pick","--abort",check=False); g(repo,"reset","--hard","HEAD"); break
        if not out(repo,"diff","--cached","--name-only") and not out(repo,"diff","--name-only"): empty.append(src); continue
        applied.append((src,commit(repo,src)))
    if out(repo,"status","--porcelain"): raise RuntimeError("dirty")
    compiled=verify(repo)
    project=next((m.group(1) for line in (repo/"pyproject.toml").read_text().splitlines() if (m:=re.fullmatch(r'version = "([^"]+)"',line))),"")
    m=re.search(r'^__version__ = "([^"]+)"',(repo/"hermes_cli/__init__.py").read_text(),re.M); cli=m.group(1) if m else ""
    if (project,cli)!=("0.21.0","0.21.0"): raise RuntimeError(f"versions {project}/{cli}")
    (report/"applied.tsv").write_text("".join(f"{a}\t{b}\n" for a,b in applied)); (report/"empty.txt").write_text("".join(x+"\n" for x in empty))
    summary={"start":START,"candidate_head":out(repo,"rev-parse","HEAD"),"candidate_tree":out(repo,"rev-parse","HEAD^{tree}"),"applied_count":len(applied),"empty_count":len(empty),"failed_commit":failed,"project_version":project,"cli_version":cli,"changed_python_compiled":compiled}
    (report/"summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n"); print(json.dumps(summary,sort_keys=True))
if __name__=="__main__": main()
