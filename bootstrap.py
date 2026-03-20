#!/usr/bin/env python3
"""TIMPAL Bootstrap Server v3.1 — version bump only, all logic identical to v3.0."""

import socket, threading, json, time, random, hashlib

PORT        = 7777
VERSION     = "3.1"
MIN_VERSION = "3.1"

GENESIS_TIME = 0   # ← REPLACE with the same number as in timpal.py

REWARD_INTERVAL=5.0; TARGET_PARTICIPANTS=10; BAN_DURATION=10
REVEAL_MISS_THRESHOLD=2; NETWORK_SIZE_SAMPLES=10
GENESIS_PREV_HASH="0"*64

def _check_genesis_time():
    if GENESIS_TIME==0:
        print("\n  "+"="*50+"\n  ERROR: GENESIS_TIME is not set.\n  "+"="*50)
        print("  Run: python3 -c \"import time; print(int(time.time()))\"")
        print("  Paste the result into both files.\n"); exit(1)

def get_current_slot(): return int((time.time()-GENESIS_TIME)/REWARD_INTERVAL)
def _ver(v):
    try: return tuple(int(x) for x in str(v).split("."))
    except: return (0,0)

def get_eligibility_threshold(n): return 1.0 if n<=TARGET_PARTICIPANTS else TARGET_PARTICIPANTS/n
def is_eligible(device_id,slot,n):
    t=get_eligibility_threshold(n)
    if t>=1.0: return True
    return int(hashlib.sha256(f"{device_id}:{slot}".encode()).hexdigest(),16)<int(t*(2**256))
def compute_collective_target(reveals):
    tickets=sorted(r["ticket"] for r in reveals.values())
    return hashlib.sha256(":".join(tickets).encode()).hexdigest()

peers={}; commits={}; reveals={}; missed_reveals={}; ban_until={}
peers_lock=threading.Lock(); lottery_lock=threading.Lock(); rate_lock=threading.Lock()
_peer_count_history=[]; _peer_count_history_lock=threading.Lock()
_chain_tip_lock=threading.Lock()
_chain_tip={"hash":GENESIS_PREV_HASH,"slot":-1,"device_id":""}
commit_ip_rate={}; reveal_ip_rate={}; hello_ip_rate={}; bs_ip_rate={}; tip_ip_rate={}
bootstrap_servers={}; bootstrap_servers_lock=threading.Lock()
COMMIT_RATE_LIMIT=3; REVEAL_RATE_LIMIT=3; HELLO_RATE_LIMIT=10
BS_RATE_LIMIT=5; TIP_RATE_LIMIT=3; BS_MAX_SERVERS=100; HELLO_PEERS_SAMPLE=50

def get_smoothed_network_size():
    with _peer_count_history_lock:
        if not _peer_count_history:
            with peers_lock: return max(1,len(peers))
        return max(1,int(sum(_peer_count_history)/len(_peer_count_history)))

def _record_network_size():
    while True:
        time.sleep(REWARD_INTERVAL)
        with peers_lock: n=len(peers)
        with _peer_count_history_lock:
            _peer_count_history.append(n)
            if len(_peer_count_history)>NETWORK_SIZE_SAMPLES: _peer_count_history.pop(0)

def _check_missed_reveals():
    last=-1
    while True:
        time.sleep(1.0); cs=get_current_slot(); chk=cs-2
        if chk<=0 or chk==last: continue
        last=chk
        with lottery_lock:
            if chk not in commits: continue
            missed=set(commits[chk].keys())-set(reveals.get(chk,{}).keys())
            for did in missed:
                missed_reveals[did]=missed_reveals.get(did,0)+1
                cnt=missed_reveals[did]
                if cnt>=REVEAL_MISS_THRESHOLD:
                    ban_until[did]=cs+BAN_DURATION; missed_reveals[did]=0
                    print(f"  [!] Reveal ban: {did[:20]}... until slot {ban_until[did]}")

def clean_old_data():
    while True:
        time.sleep(60); now=time.time(); cutoff=now-300; cs=get_current_slot()
        with peers_lock:
            stale=[p for p,d in peers.items() if d["last_seen"]<cutoff]
            for p in stale: del peers[p]
            if stale: print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")
        with lottery_lock:
            for d in (commits,reveals):
                for s in [s for s in list(d) if s<cs-20]: del d[s]
            for did in [d for d,s in list(ban_until.items()) if s<cs]:
                del ban_until[did]; missed_reveals.pop(did,None)
        with rate_lock:
            for rd in (commit_ip_rate,reveal_ip_rate,tip_ip_rate):
                for ip in list(rd.keys()):
                    for s in [s for s in list(rd[ip].keys()) if s<cs-20]: del rd[ip][s]
                    if not rd[ip]: del rd[ip]
            for ip in list(hello_ip_rate.keys()):
                hello_ip_rate[ip]=[t for t in hello_ip_rate[ip] if now-t<60]
                if not hello_ip_rate[ip]: del hello_ip_rate[ip]
        bs_cut=now-86400
        with bootstrap_servers_lock:
            stale_bs=[k for k,v in bootstrap_servers.items() if v["last_seen"]<bs_cut]
            for k in stale_bs: del bootstrap_servers[k]
            for ip in list(bs_ip_rate.keys()):
                bs_ip_rate[ip]=[t for t in bs_ip_rate[ip] if now-t<3600]
                if not bs_ip_rate[ip]: del bs_ip_rate[ip]

def handle_client(conn,addr):
    try:
        conn.settimeout(10.0); data=b""
        while True:
            chunk=conn.recv(65536)
            if not chunk: break
            data+=chunk
            if len(data)>131072: break
        msg=json.loads(data.decode()); mt=msg.get("type"); ip=addr[0]

        if mt=="HELLO":
            did=msg.get("device_id",""); port=msg.get("port",PORT); ver=msg.get("version","0.0")
            if _ver(ver)<_ver(MIN_VERSION):
                conn.sendall(json.dumps({"type":"VERSION_REJECTED","reason":f"Update required (min {MIN_VERSION}) — delete wallet+ledger then re-download from GitHub"}).encode()); return
            now=time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip,[])
                hello_ip_rate[ip]=[t for t in hello_ip_rate[ip] if now-t<60]
                if len(hello_ip_rate[ip])>=HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                hello_ip_rate[ip].append(now)
            ns=get_smoothed_network_size()
            with peers_lock:
                is_new=did not in peers
                if is_new and len(peers)>=10000:
                    del peers[min(peers,key=lambda k:peers[k]["last_seen"])]
                peers[did]={"ip":ip,"port":port,"last_seen":time.time()}
                ap=[{"device_id":p,"ip":d["ip"],"port":d["port"]} for p,d in peers.items() if p!=did]
                pl=random.sample(ap,min(HELLO_PEERS_SAMPLE,len(ap)))
            with _chain_tip_lock: th=_chain_tip["hash"]; ts=_chain_tip["slot"]
            conn.sendall(json.dumps({"type":"PEERS","peers":pl,"network_size":ns,"chain_tip_hash":th,"chain_tip_slot":ts}).encode())
            if is_new: print(f"  [+] Node v{ver}: {did[:20]}... from {ip}:{port} | total={len(peers)} ns={ns}")

        elif mt=="SUBMIT_TIP":
            did=msg.get("device_id",""); slot=msg.get("slot"); th=msg.get("tip_hash","")
            if not all([did,slot is not None,th]): conn.sendall(json.dumps({"type":"ERROR","msg":"missing"}).encode()); return
            if not isinstance(slot,int) or slot<0: conn.sendall(json.dumps({"type":"ERROR","msg":"bad slot"}).encode()); return
            if len(did)!=64 or not all(c in "0123456789abcdef" for c in did.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad id"}).encode()); return
            if len(th)!=64 or not all(c in "0123456789abcdef" for c in th.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad hash"}).encode()); return
            cs=get_current_slot()
            if abs(slot-cs)>10: conn.sendall(json.dumps({"type":"ERROR","msg":"stale"}).encode()); return
            with rate_lock:
                tip_ip_rate.setdefault(ip,{}); tip_ip_rate[ip].setdefault(slot,0)
                if tip_ip_rate[ip][slot]>=TIP_RATE_LIMIT: conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                tip_ip_rate[ip][slot]+=1
            with _chain_tip_lock:
                if slot>_chain_tip["slot"]:
                    _chain_tip.update({"hash":th,"slot":slot,"device_id":did})
                    print(f"  [chain] Tip: slot {slot} by {did[:20]}...")
            conn.sendall(json.dumps({"type":"TIP_ACK","slot":slot}).encode())

        elif mt=="GET_CHAIN_TIP":
            with _chain_tip_lock: th=_chain_tip["hash"]; ts=_chain_tip["slot"]
            conn.sendall(json.dumps({"type":"CHAIN_TIP_RESPONSE","chain_tip_hash":th,"chain_tip_slot":ts}).encode())

        elif mt=="SUBMIT_COMMIT":
            did=msg.get("device_id",""); slot=msg.get("slot"); commit=msg.get("commit","")
            if not all([did,slot is not None,commit]): conn.sendall(json.dumps({"type":"ERROR","msg":"missing"}).encode()); return
            if not isinstance(slot,int) or slot<0: conn.sendall(json.dumps({"type":"ERROR","msg":"bad slot"}).encode()); return
            if len(did)!=64 or not all(c in "0123456789abcdef" for c in did.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad id"}).encode()); return
            if len(commit)!=64 or not all(c in "0123456789abcdef" for c in commit.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad commit"}).encode()); return
            cs=get_current_slot()
            if abs(slot-cs)>2: conn.sendall(json.dumps({"type":"ERROR","msg":"stale"}).encode()); return
            with lottery_lock: ban=ban_until.get(did,0)
            if ban>=cs: conn.sendall(json.dumps({"type":"COMMIT_REJECTED","reason":"ban","ban_until":ban}).encode()); return
            ns=get_smoothed_network_size()
            if not is_eligible(did,slot,ns): conn.sendall(json.dumps({"type":"COMMIT_REJECTED","reason":"not eligible"}).encode()); return
            with rate_lock:
                commit_ip_rate.setdefault(ip,{}); commit_ip_rate[ip].setdefault(slot,0)
                if commit_ip_rate[ip][slot]>=COMMIT_RATE_LIMIT: conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                commit_ip_rate[ip][slot]+=1
            with peers_lock:
                if did in peers: peers[did]["last_seen"]=time.time()
            with lottery_lock:
                commits.setdefault(slot,{})
                if did not in commits[slot]:
                    commits[slot][did]=commit
                    print(f"  [slot {slot}] Commit: {did[:20]}... ({len(commits[slot])} total) ns={ns}")
            conn.sendall(json.dumps({"type":"COMMIT_ACK","slot":slot,"network_size":ns}).encode())

        elif mt=="SUBMIT_REVEAL":
            did=msg.get("device_id",""); slot=msg.get("slot"); ticket=msg.get("ticket","")
            sig=msg.get("sig",""); seed=msg.get("seed",""); pk=msg.get("public_key","")
            if not all([did,slot is not None,ticket,sig,seed,pk]): conn.sendall(json.dumps({"type":"ERROR","msg":"missing"}).encode()); return
            if not isinstance(slot,int) or slot<0: conn.sendall(json.dumps({"type":"ERROR","msg":"bad slot"}).encode()); return
            if len(did)!=64 or not all(c in "0123456789abcdef" for c in did.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad id"}).encode()); return
            if len(ticket)!=64 or not all(c in "0123456789abcdef" for c in ticket.lower()): conn.sendall(json.dumps({"type":"ERROR","msg":"bad ticket"}).encode()); return
            if not isinstance(seed,str) or seed!=str(slot): conn.sendall(json.dumps({"type":"ERROR","msg":"bad seed"}).encode()); return
            if len(pk)>8192 or len(sig)>8192: conn.sendall(json.dumps({"type":"ERROR","msg":"too large"}).encode()); return
            cs=get_current_slot()
            if abs(slot-cs)>2: conn.sendall(json.dumps({"type":"ERROR","msg":"stale"}).encode()); return
            with rate_lock:
                reveal_ip_rate.setdefault(ip,{}); reveal_ip_rate[ip].setdefault(slot,0)
                if reveal_ip_rate[ip][slot]>=REVEAL_RATE_LIMIT: conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                reveal_ip_rate[ip][slot]+=1
            with peers_lock:
                if did in peers: peers[did]["last_seen"]=time.time()
            with lottery_lock:
                if slot in commits and did in commits[slot]:
                    reveals.setdefault(slot,{})
                    if did not in reveals[slot]:
                        reveals[slot][did]={"ticket":ticket,"sig":sig,"seed":seed,"public_key":pk}
                        print(f"  [slot {slot}] Reveal: {did[:20]}... ({len(reveals[slot])} total)")
            conn.sendall(json.dumps({"type":"REVEAL_ACK","slot":slot}).encode())

        elif mt=="GET_COMMITS":
            slot=msg.get("slot")
            if slot is None: conn.sendall(json.dumps({"type":"ERROR","msg":"missing slot"}).encode()); return
            with lottery_lock: sc=dict(commits.get(slot,{}))
            conn.sendall(json.dumps({"type":"COMMITS_RESPONSE","slot":slot,"commits":sc}).encode())

        elif mt=="GET_REVEALS":
            slot=msg.get("slot")
            if slot is None: conn.sendall(json.dumps({"type":"ERROR","msg":"missing slot"}).encode()); return
            with lottery_lock: sr=dict(reveals.get(slot,{}))
            ct=compute_collective_target(sr) if sr else None
            conn.sendall(json.dumps({"type":"REVEALS_RESPONSE","slot":slot,"reveals":sr,"collective_target":ct}).encode())

        elif mt=="PING":
            did=msg.get("device_id","")
            with peers_lock:
                if did in peers: peers[did]["last_seen"]=time.time()
            conn.sendall(json.dumps({"type":"PONG","network_size":get_smoothed_network_size()}).encode())

        elif mt=="GET_PEERS":
            with peers_lock: pl=[{"device_id":p} for p in peers]
            conn.sendall(json.dumps({"type":"PEERS","peers":pl}).encode())

        elif mt=="REGISTER_BOOTSTRAP":
            h=msg.get("host","").strip(); p=msg.get("port",0)
            if not h or not isinstance(p,int) or not(1024<=p<=65535): conn.sendall(json.dumps({"type":"ERROR","msg":"invalid"}).encode()); return
            if len(h)>253: conn.sendall(json.dumps({"type":"ERROR","msg":"invalid host"}).encode()); return
            now=time.time(); known=set()
            with bootstrap_servers_lock: hl=[v["host"] for v in bootstrap_servers.values()]
            for host in hl:
                try: known.add(socket.gethostbyname(host))
                except: known.add(host)
            if ip not in known:
                with bootstrap_servers_lock:
                    bs_ip_rate.setdefault(ip,[])
                    bs_ip_rate[ip]=[t for t in bs_ip_rate[ip] if now-t<3600]
                    if len(bs_ip_rate[ip])>=BS_RATE_LIMIT: conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                    bs_ip_rate[ip].append(now)
            key=f"{h}:{p}"
            try:
                pr=socket.socket(socket.AF_INET,socket.SOCK_STREAM); pr.settimeout(3.0); pr.connect((h,p)); pr.close()
            except: conn.sendall(json.dumps({"type":"ERROR","msg":"not reachable"}).encode()); return
            with bootstrap_servers_lock:
                if key not in bootstrap_servers:
                    if len(bootstrap_servers)>=BS_MAX_SERVERS:
                        del bootstrap_servers[min(bootstrap_servers,key=lambda k:bootstrap_servers[k]["last_seen"])]
                    bootstrap_servers[key]={"host":h,"port":p,"last_seen":now}
                    print(f"  [+] Bootstrap: {key} | total={len(bootstrap_servers)}")
                else: bootstrap_servers[key]["last_seen"]=now
            conn.sendall(json.dumps({"type":"REGISTER_ACK","key":key}).encode())

        elif mt=="GET_BOOTSTRAP_SERVERS":
            now=time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip,[])
                hello_ip_rate[ip]=[t for t in hello_ip_rate[ip] if now-t<60]
                if len(hello_ip_rate[ip])>=HELLO_RATE_LIMIT: conn.sendall(json.dumps({"type":"ERROR","msg":"rate limit"}).encode()); return
                hello_ip_rate[ip].append(now)
            with bootstrap_servers_lock: bl=[{"host":v["host"],"port":v["port"]} for v in bootstrap_servers.values()]
            conn.sendall(json.dumps({"type":"BOOTSTRAP_SERVERS_RESPONSE","servers":bl}).encode())

    except Exception: pass
    finally: conn.close()

def _gossip_bootstrap_servers():
    time.sleep(30)
    while True:
        time.sleep(300)
        with bootstrap_servers_lock: targets=list(bootstrap_servers.values()); ol=list(bootstrap_servers.values())
        for t in targets:
            for e in ol:
                if e["host"]==t["host"] and e["port"]==t["port"]: continue
                try:
                    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(3.0)
                    s.connect((t["host"],t["port"])); s.sendall(json.dumps({"type":"REGISTER_BOOTSTRAP","host":e["host"],"port":e["port"]}).encode()); s.close()
                except: continue

def main():
    _check_genesis_time()
    print("="*54)
    print("  TIMPAL Bootstrap Server v3.1")
    print("  Peer Discovery + Eligibility-Gated Lottery + Chain Tip")
    print("="*54)
    print(f"  Port: {PORT} | Min version: {MIN_VERSION} | Target: {TARGET_PARTICIPANTS}/slot")
    print("="*54+"\n")
    for fn in (clean_old_data,_gossip_bootstrap_servers,_record_network_size,_check_missed_reveals):
        threading.Thread(target=fn,daemon=True).start()
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(("",PORT)); srv.listen(200)
    print("  Ready. Waiting for nodes...\n")
    sem=threading.Semaphore(200)
    def _wrap(conn,addr):
        try: handle_client(conn,addr)
        finally: sem.release()
    while True:
        try:
            conn,addr=srv.accept()
            if not sem.acquire(blocking=False): conn.close(); continue
            threading.Thread(target=_wrap,args=(conn,addr),daemon=True).start()
        except KeyboardInterrupt: print("\n  Shutting down."); break
        except: continue

if __name__=="__main__": main()
