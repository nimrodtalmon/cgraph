"""Contribution Graph: contribute, curate, export a front.

A contribution is either a node (a model, a theorem, a dataset, ...) or an arc
(one node builds on another, e.g. an experiment "validates" a model). Curators
accept contributions; an accepted arc needs both of its endpoints accepted.
The front of a curator: accepted nodes that no accepted arc points to.

Standard library only. Usage: python server.py [port]   (data in $DATA)
"""
import json
import math
import os
import random
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4801
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA", os.path.join(HERE, "data"))
os.makedirs(DATA, exist_ok=True)
DB_PATH = os.path.join(DATA, "cgraph2.db")
LOCK = threading.Lock()
VENDOR = {"cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js"}

TYPES = ["problem", "assumption", "model", "theorem", "algorithm",
         "dataset", "experiment", "claim"]

# Labels an arc from one node type to another may carry ("builds on" always).
SCHEMA = {
    ("model", "problem"): ["addresses"],
    ("model", "model"): ["extends", "generalizes"],
    ("model", "assumption"): ["relies on"],
    ("theorem", "model"): ["about"],
    ("theorem", "assumption"): ["relies on"],
    ("theorem", "theorem"): ["uses", "generalizes"],
    ("theorem", "algorithm"): ["analyzes"],
    ("algorithm", "problem"): ["solves"],
    ("algorithm", "model"): ["works in"],
    ("algorithm", "algorithm"): ["improves"],
    ("experiment", "model"): ["validates", "refutes"],
    ("experiment", "dataset"): ["uses"],
    ("experiment", "algorithm"): ["evaluates"],
    ("experiment", "claim"): ["supports", "refutes"],
    ("experiment", "theorem"): ["illustrates"],
    ("claim", "theorem"): ["follows from"],
    ("claim", "experiment"): ["follows from"],
    ("dataset", "problem"): ["addresses"],
}
GENERIC = "builds on"


def labels(src_type, dst_type):
    return SCHEMA.get((src_type, dst_type), []) + [GENERIC]


class Bad(Exception):
    pass


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init():
    con = db()
    with con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS graphs(id TEXT PRIMARY KEY, name TEXT, sample INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS nodes(
            id INTEGER PRIMARY KEY, graph TEXT, type TEXT, title TEXT, content TEXT,
            author TEXT, token TEXT, x REAL, y REAL, created REAL);
        CREATE TABLE IF NOT EXISTS arcs(
            id INTEGER PRIMARY KEY, graph TEXT, src INTEGER, dst INTEGER, label TEXT,
            author TEXT, token TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS curators(
            id INTEGER PRIMARY KEY, graph TEXT, name TEXT, key TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS accepted(
            curator INTEGER, kind TEXT, item INTEGER, PRIMARY KEY(curator, kind, item));
        CREATE INDEX IF NOT EXISTS nodes_g ON nodes(graph);
        CREATE INDEX IF NOT EXISTS arcs_g ON arcs(graph);
        """)
        con.execute("INSERT OR IGNORE INTO graphs VALUES('main','Main graph',0,?)", (time.time(),))
    con.close()


def text(v, name, lo, hi):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise Bad(f"{name} must be text")
    v = v.strip()
    if not lo <= len(v) <= hi:
        raise Bad(f"{name}: {lo}-{hi} characters" if lo else f"{name}: at most {hi} characters")
    return v


def num(v, lo, hi, default=None):
    if v is None:
        return default
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise Bad("bad number")
    return max(lo, min(hi, float(v)))


def get_graph(con, gid):
    row = con.execute("SELECT * FROM graphs WHERE id=?", (gid,)).fetchone()
    if not row:
        raise Bad("unknown graph")
    return dict(row)


def load(con, gid):
    nodes = {r["id"]: dict(r) for r in con.execute("SELECT * FROM nodes WHERE graph=?", (gid,))}
    arcs = {r["id"]: dict(r) for r in con.execute("SELECT * FROM arcs WHERE graph=?", (gid,))}
    return nodes, arcs


def public(d):
    return {k: v for k, v in d.items() if k not in ("token", "graph")}


def snapshot(con, gid):
    g = get_graph(con, gid)
    nodes, arcs = load(con, gid)
    accepted_by = {}
    for r in con.execute("SELECT a.kind, a.item FROM accepted a JOIN curators c ON c.id=a.curator WHERE c.graph=?", (gid,)):
        accepted_by[(r["kind"], r["item"])] = accepted_by.get((r["kind"], r["item"]), 0) + 1
    out_nodes = []
    for n in nodes.values():
        d = public(n)
        d["accepts"] = accepted_by.get(("n", n["id"]), 0)
        out_nodes.append(d)
    out_arcs = []
    for a in arcs.values():
        d = public(a)
        d["accepts"] = accepted_by.get(("a", a["id"]), 0)
        out_arcs.append(d)
    curators = [{"id": r["id"], "name": r["name"]}
                for r in con.execute("SELECT id, name FROM curators WHERE graph=? ORDER BY id", (gid,))]
    return {"graph": {"id": g["id"], "name": g["name"], "sample": bool(g["sample"])},
            "nodes": out_nodes, "arcs": out_arcs, "curators": curators, "types": TYPES,
            "schema": [{"src": s, "dst": d, "labels": l} for (s, d), l in SCHEMA.items()],
            "generic": GENERIC}


def reaches(arcs, start, goal):
    out = {}
    for a in arcs.values():
        out.setdefault(a["src"], []).append(a["dst"])
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        if x == goal:
            return True
        if x not in seen:
            seen.add(x)
            stack.extend(out.get(x, []))
    return False


# --- contributing --------------------------------------------------------------

def add_node(con, gid, b):
    get_graph(con, gid)
    typ = b.get("type")
    if typ not in TYPES:
        raise Bad("unknown type")
    title = text(b.get("title"), "Title", 1, 120)
    content = text(b.get("content"), "Details", 0, 4000)
    author = text(b.get("author"), "Name", 1, 60)
    x, y = num(b.get("x"), -1e6, 1e6), num(b.get("y"), -1e6, 1e6)
    token = secrets.token_urlsafe(12)
    cur = con.execute("INSERT INTO nodes(graph,type,title,content,author,token,x,y,created) VALUES(?,?,?,?,?,?,?,?,?)",
                      (gid, typ, title, content, author, token, x, y, time.time()))
    return {"id": cur.lastrowid, "token": token}


def add_arc(con, gid, b):
    nodes, arcs = load(con, gid)
    src, dst = b.get("src"), b.get("dst")
    if src not in nodes or dst not in nodes:
        raise Bad("unknown node")
    if src == dst:
        raise Bad("an arc needs two different nodes")
    label = b.get("label")
    if label not in labels(nodes[src]["type"], nodes[dst]["type"]):
        raise Bad(f"“{label}” is not allowed from {nodes[src]['type']} to {nodes[dst]['type']}")
    if any(a["src"] == src and a["dst"] == dst and a["label"] == label for a in arcs.values()):
        raise Bad("this arc already exists")
    if reaches(arcs, dst, src):
        raise Bad("this arc would close a cycle")
    author = text(b.get("author"), "Name", 1, 60)
    token = secrets.token_urlsafe(12)
    cur = con.execute("INSERT INTO arcs(graph,src,dst,label,author,token,created) VALUES(?,?,?,?,?,?,?)",
                      (gid, src, dst, label, author, token, time.time()))
    return {"id": cur.lastrowid, "token": token}


def own(con, gid, b):
    kind, item = b.get("kind"), b.get("id")
    table = {"n": "nodes", "a": "arcs"}.get(kind)
    if not table:
        raise Bad("bad kind")
    row = con.execute(f"SELECT * FROM {table} WHERE id=? AND graph=?", (item, gid)).fetchone()
    if not row:
        raise Bad("unknown contribution")
    if not isinstance(b.get("token"), str) or not secrets.compare_digest(row["token"], b["token"]):
        raise Bad("only its contributor can change this")
    if con.execute("SELECT 1 FROM accepted WHERE kind=? AND item=?", (kind, item)).fetchone():
        raise Bad("already accepted by a curator, so it cannot change")
    return kind, item, dict(row)


def edit(con, gid, b):
    kind, item, row = own(con, gid, b)
    if kind != "n":
        raise Bad("arcs cannot be edited; delete and add again")
    title = text(b.get("title", row["title"]), "Title", 1, 120)
    content = text(b.get("content", row["content"]), "Details", 0, 4000)
    con.execute("UPDATE nodes SET title=?, content=? WHERE id=?", (title, content, item))
    return {"ok": True}


def delete(con, gid, b):
    kind, item, _ = own(con, gid, b)
    if kind == "n":
        if con.execute("SELECT 1 FROM arcs WHERE src=? OR dst=?", (item, item)).fetchone():
            raise Bad("remove its arcs first")
        con.execute("DELETE FROM nodes WHERE id=?", (item,))
    else:
        con.execute("DELETE FROM arcs WHERE id=?", (item,))
    return {"ok": True}


def positions(con, gid, b):
    pos = b.get("pos")
    if not isinstance(pos, list) or len(pos) > 2000:
        raise Bad("bad positions")
    for p in pos:
        if not isinstance(p, dict):
            raise Bad("bad positions")
        con.execute("UPDATE nodes SET x=?, y=? WHERE id=? AND graph=?",
                    (num(p.get("x"), -1e6, 1e6, 0), num(p.get("y"), -1e6, 1e6, 0), p.get("id"), gid))
    return {"ok": True}


# --- curating ------------------------------------------------------------------

def add_curator(con, gid, b, key=None):
    get_graph(con, gid)
    name = text(b.get("name"), "Name", 1, 60)
    key = key or secrets.token_urlsafe(12)
    cur = con.execute("INSERT INTO curators(graph,name,key,created) VALUES(?,?,?,?)", (gid, name, key, time.time()))
    return {"id": cur.lastrowid, "name": name, "key": key}


def curator_row(con, gid, cid):
    row = con.execute("SELECT * FROM curators WHERE id=? AND graph=?", (cid, gid)).fetchone()
    if not row:
        raise Bad("unknown curator")
    return row


def acc_sets(con, cid):
    rows = con.execute("SELECT kind, item FROM accepted WHERE curator=?", (cid,)).fetchall()
    return ({r["item"] for r in rows if r["kind"] == "n"}, {r["item"] for r in rows if r["kind"] == "a"})


def front(arcs, acc_n, acc_a):
    built_on = {arcs[a]["dst"] for a in acc_a if a in arcs}
    return sorted(n for n in acc_n if n not in built_on)


def view(con, gid, cid):
    row = curator_row(con, gid, cid)
    _, arcs = load(con, gid)
    acc_n, acc_a = acc_sets(con, cid)
    return {"id": row["id"], "name": row["name"], "nodes": sorted(acc_n), "arcs": sorted(acc_a),
            "front": front(arcs, acc_n, acc_a)}


def check_key(con, gid, cid, key):
    row = curator_row(con, gid, cid)
    if not isinstance(key, str) or not secrets.compare_digest(row["key"], key):
        raise Bad("wrong curator key")


def effect(nodes, arcs, acc_n, acc_a, accept, kind, item):
    """(nodes, arcs) changed by accepting / un-accepting (kind, item)."""
    if accept:
        if kind == "n":
            return {item} - acc_n, set()
        a = arcs[item]
        return {a["src"], a["dst"]} - acc_n, {item} - acc_a
    if kind == "a":
        return set(), {item} & acc_a
    touching = {a for a in acc_a if arcs[a]["src"] == item or arcs[a]["dst"] == item}
    return {item} & acc_n, touching


def curate(con, gid, cid, b, accept):
    check_key(con, gid, cid, b.get("key"))
    nodes, arcs = load(con, gid)
    kind, item = b.get("kind"), b.get("id")
    if kind not in ("n", "a") or item not in (nodes if kind == "n" else arcs):
        raise Bad("unknown contribution")
    acc_n, acc_a = acc_sets(con, cid)
    ch_n, ch_a = effect(nodes, arcs, acc_n, acc_a, accept, kind, item)
    if b.get("preview"):
        return {"nodes": sorted(ch_n), "arcs": sorted(ch_a)}
    for n in ch_n:
        con.execute("INSERT OR IGNORE INTO accepted VALUES(?,?,?)" if accept else
                    "DELETE FROM accepted WHERE curator=? AND kind=? AND item=?", (cid, "n", n))
    for a in ch_a:
        con.execute("INSERT OR IGNORE INTO accepted VALUES(?,?,?)" if accept else
                    "DELETE FROM accepted WHERE curator=? AND kind=? AND item=?", (cid, "a", a))
    return view(con, gid, cid)


def restore(con, gid, cid, b):
    """Set the accepted state wholesale (used for undo)."""
    check_key(con, gid, cid, b.get("key"))
    nodes, arcs = load(con, gid)
    want_n = {n for n in (b.get("nodes") or []) if n in nodes}
    want_a = {a for a in (b.get("arcs") or []) if a in arcs and arcs[a]["src"] in want_n and arcs[a]["dst"] in want_n}
    con.execute("DELETE FROM accepted WHERE curator=?", (cid,))
    con.executemany("INSERT INTO accepted VALUES(?,?,?)", [(cid, "n", n) for n in want_n] + [(cid, "a", a) for a in want_a])
    return view(con, gid, cid)


def export(con, gid, cid):
    v = view(con, gid, cid)
    nodes, arcs = load(con, gid)
    return {
        "graph": gid, "curator": v["name"],
        "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "front": v["front"],
        "nodes": [{k: nodes[n][k] for k in ("id", "type", "title", "content", "author")} for n in v["nodes"]],
        "arcs": [{k: arcs[a][k] for k in ("id", "src", "dst", "label", "author")} for a in v["arcs"]],
    }


# --- samples -------------------------------------------------------------------

WEIGHTS = {"problem": 1, "assumption": 1.5, "model": 2, "theorem": 2, "algorithm": 1,
           "dataset": 1, "experiment": 2, "claim": 1}
NAMES = ["Ada", "Ben", "Chen", "Dana", "Eli", "Farah", "Gil", "Hana", "Ivo", "Jun"]


def sample(con, b):
    n = int(num(b.get("nodes"), 2, 150, 25))
    per = num(b.get("arcs"), 0, 3, 1.5)
    p = num(b.get("accept"), 0, 1, 0.5)
    gid = "s-" + secrets.token_hex(4)
    con.execute("INSERT INTO graphs VALUES(?,?,1,?)", (gid, f"Sample · {n} nodes", time.time()))
    rng = random.Random()
    types, counts, ids = [], {}, []
    now = time.time()
    for i in range(n):
        t = rng.choices(TYPES, weights=[WEIGHTS[x] for x in TYPES])[0]
        counts[t] = counts.get(t, 0) + 1
        cur = con.execute("INSERT INTO nodes(graph,type,title,content,author,token,x,y,created) VALUES(?,?,?,?,?,?,NULL,NULL,?)",
                          (gid, t, f"{t.capitalize()} {counts[t]}", "", rng.choice(NAMES), secrets.token_urlsafe(12), now))
        types.append(t)
        ids.append(cur.lastrowid)
    arcs = []
    for i in range(1, n):
        k = min(i, int(per) + (1 if rng.random() < per - int(per) else 0))
        cands = list(range(i))
        w = [4 if (types[i], types[j]) in SCHEMA else 1 for j in cands]
        chosen = set()
        while len(chosen) < k:
            chosen.add(rng.choices(cands, weights=w)[0])
        for j in chosen:
            label = rng.choice(SCHEMA.get((types[i], types[j]), [GENERIC]))
            cur = con.execute("INSERT INTO arcs(graph,src,dst,label,author,token,created) VALUES(?,?,?,?,?,?,?)",
                              (gid, ids[i], ids[j], label, rng.choice(NAMES), secrets.token_urlsafe(12), now))
            arcs.append((cur.lastrowid, ids[i], ids[j]))
    c = add_curator(con, gid, {"name": "Sample curator"})
    acc = {x for x in ids if rng.random() < p}
    for x in acc:
        con.execute("INSERT INTO accepted VALUES(?,?,?)", (c["id"], "n", x))
    for a, s, d in arcs:
        if s in acc and d in acc and rng.random() < max(p, 0.3):
            con.execute("INSERT INTO accepted VALUES(?,?,?)", (c["id"], "a", a))
    return {"graph": gid, "curator": c}


# --- http ----------------------------------------------------------------------

WRITES = {}


def allow(ip, limit=600, window=600):
    now = time.time()
    hist = [t for t in WRITES.get(ip, []) if now - t < window]
    ok = len(hist) < limit
    if ok:
        hist.append(now)
    WRITES[ip] = hist
    return ok


class Handler(BaseHTTPRequestHandler):
    server_version = "cgraph"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def send(self, code, obj=None, body=None, ctype="application/json", extra=None):
        data = body if body is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def handle_api(self, p, b):
        con = db()
        try:
            with con:
                if p == ["api", "sample"] and b is not None:
                    return sample(con, b)
                if len(p) < 3 or p[1] != "g":
                    raise LookupError
                gid, rest = p[2], p[3:]
                if b is None:  # GET
                    if rest == []:
                        return snapshot(con, gid)
                    if len(rest) == 2 and rest[0] == "curator":
                        return view(con, gid, int(rest[1]))
                    if len(rest) == 3 and rest[0] == "curator" and rest[2] == "export":
                        return ("download", export(con, gid, int(rest[1])))
                    raise LookupError
                if rest == ["node"]:
                    return add_node(con, gid, b)
                if rest == ["arc"]:
                    return add_arc(con, gid, b)
                if rest == ["edit"]:
                    return edit(con, gid, b)
                if rest == ["delete"]:
                    return delete(con, gid, b)
                if rest == ["positions"]:
                    return positions(con, gid, b)
                if rest == ["curator"]:
                    return add_curator(con, gid, b)
                if len(rest) == 3 and rest[0] == "curator":
                    cid, act = int(rest[1]), rest[2]
                    if act == "check":
                        check_key(con, gid, cid, b.get("key"))
                        return {"ok": True}
                    if act in ("accept", "unaccept"):
                        return curate(con, gid, cid, b, act == "accept")
                    if act == "restore":
                        return restore(con, gid, cid, b)
                raise LookupError
        finally:
            con.close()

    def do_GET(self):
        p = self.parts()
        if p in ([], ["index.html"]):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, body=f.read(), ctype="text/html; charset=utf-8")
        if len(p) == 2 and p[0] == "vendor" and p[1] in VENDOR:
            with open(os.path.join(HERE, "vendor", p[1]), "rb") as f:
                return self.send(200, body=f.read(), ctype="application/javascript",
                                 extra={"Cache-Control": "public, max-age=86400"})
        try:
            with LOCK:
                r = self.handle_api(p, None)
            if isinstance(r, tuple):
                return self.send(200, body=json.dumps(r[1], indent=2).encode(), extra={
                    "Content-Disposition": f'attachment; filename="front-{p[2]}-{p[4]}.json"'})
            self.send(200, r)
        except LookupError:
            self.send(404, {"error": "not found"})
        except (Bad, ValueError) as e:
            self.send(400, {"error": str(e)})

    def do_POST(self):
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        if not allow(ip):
            return self.send(429, {"error": "too many requests; try again in a few minutes"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 100000:
                raise Bad("request too large")
            b = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(b, dict):
                raise Bad("bad request")
            with LOCK:
                self.send(200, self.handle_api(self.parts(), b))
        except LookupError:
            self.send(404, {"error": "not found"})
        except (Bad, ValueError, KeyError, TypeError) as e:
            self.send(400, {"error": str(e)})


if __name__ == "__main__":
    init()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
