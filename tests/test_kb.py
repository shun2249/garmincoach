"""知識庫發布：float16 向量載入、export/import 打包往返（sha256、FTS 重建、覆蓋保護）、論文清單寫讀與照清單重建。假論文、假向量，不打 PubMed / OpenAI。"""
import json, shutil, sqlite3, sys, tempfile
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach import knowledge as K
from garmin_coach.knowledge import KnowledgeBase, EMBED_DIM

tmp = Path(tempfile.mkdtemp(prefix="kb_test_"))
K.KB_RAW = tmp / "raw"          # add_papers 會寫原始 JSON

PAPERS = [
    {"pmid": "1001", "title": "Polarized training in recreational runners: a meta-analysis", "abstract": "Polarized intensity distribution improved time-trial performance.",
     "authors": ["Chen A", "Lin B"], "journal": "J Test Sci", "year": 2023, "doi": "10.1/x1", "pmcid": "PMC1001", "pub_types": ["Meta-Analysis", "Journal Article"], "mesh": ["Running"]},
    {"pmid": "1002", "title": "Tapering before marathon: randomized trial", "abstract": "Two-week taper reduced volume by half.",
     "authors": ["Wu C"], "journal": "J Test Sci", "year": 2022, "doi": "10.1/x2", "pmcid": "PMC1002", "pub_types": ["Randomized Controlled Trial"], "mesh": []},
    {"pmid": "1003", "title": "Achilles tendinopathy load management review", "abstract": "Progressive loading is first-line treatment.",
     "authors": ["Hsu D", "Lee E", "Kao F"], "journal": "Sports Med Test", "year": 2024, "doi": None, "pmcid": None, "pub_types": ["Review"], "mesh": []},
]
SECTIONS = [("Methods", "Participants ran polarized sessions three times weekly for twelve weeks. " * 8),
            ("Results", "Time-trial performance improved by four percent versus threshold training. " * 8)]

def fake_vectors(kb):
    """每段一個固定亂數單位向量（seed = chunk id），寫成 float32。"""
    rows = kb.conn.execute("SELECT id FROM kb_chunks").fetchall()
    for r in rows:
        v = np.random.default_rng(r["id"]).standard_normal(EMBED_DIM).astype(np.float32); v /= np.linalg.norm(v)
        kb.conn.execute("UPDATE kb_chunks SET embedding=? WHERE id=?", (v.tobytes(), r["id"]))
    kb.conn.commit(); kb._matrix = None

# 1) 建小知識庫
kb = KnowledgeBase(tmp / "k.db")
assert kb.add_papers(PAPERS[:2], "zone2") == 2 and kb.add_papers(PAPERS[2:], "injury_running") == 1
kb.add_topics("1001", ["interval"]); kb.add_topics("9999", ["x"])          # 不存在的 pmid 不炸
kb.add_fulltext("1001", SECTIONS); kb.mark_fulltext("1002", "none")
fake_vectors(kb)
st = kb.stats()
assert st["papers"] == 3 and st["with_fulltext"] == 1 and st["chunks"] == 5 and st["embedded"] == 5, st
assert json.loads(kb.conn.execute("SELECT topics FROM kb_papers WHERE pmid='1001'").fetchone()[0]) == ["interval", "zone2"]
qvec = K._vec(kb.conn.execute("SELECT embedding FROM kb_chunks WHERE pmid='1002'").fetchone()[0])
hits = kb.search("taper", qvec=qvec, k=3)
assert hits[0]["pmid"] == "1002" and hits[0]["citation"].startswith("Wu (2022)"), hits[0]
assert kb.nearest(qvec, k=1)[0]["pmid"] == "1002"
assert kb.search("polarized", k=3)[0]["pmid"] == "1001"                    # 純 BM25（沒給向量）
print("build + search ok")

# 2) export → import 往返（分片切小，驗證接回來）
K.PACK_PART_BYTES = 4096
pack = tmp / "pack" / "knowledge.db.xz"
rep = K.export_pack(kb.path, pack)
assert pack.exists() and pack.with_name("knowledge.db.xz.sha256").exists() and rep["papers"] == 3 and rep["chunks"] == 5 and rep["fp16"], rep
parts = sorted(pack.parent.glob("knowledge.db.xz.[0-9][0-9][0-9]"))
assert len(parts) >= 2 and [p.name for p in parts] == [f"knowledge.db.xz.{i:03d}" for i in range(1, len(parts) + 1)] and len(rep["parts"]) == len(parts)
assert b"".join(p.read_bytes() for p in parts) == pack.read_bytes()
assert rep["sha256"] == pack.with_name("knowledge.db.xz.sha256").read_text().split()[0]
dst = tmp / "k2.db"
r = K.import_pack(str(pack), dst=dst)
assert r["verified"] and r["stats"]["papers"] == 3 and r["stats"]["embedded"] == 5 and r["stats"]["with_fulltext"] == 1, r
kb2 = KnowledgeBase(dst)
assert kb2.conn.execute("SELECT length(embedding) FROM kb_chunks LIMIT 1").fetchone()[0] == EMBED_DIM * 2   # float16
assert kb2.search("taper", qvec=qvec, k=3)[0]["pmid"] == "1002" and kb2.nearest(qvec, k=1)[0]["sim"] > 0.99
assert kb2.search("polarized", k=3)[0]["pmid"] == "1001"                   # FTS 有重建
assert kb2.conn.execute("SELECT COUNT(*) FROM kb_fts WHERE kb_fts MATCH 'taper'").fetchone()[0] == 1
# 混用：新 embedding 是 float32，舊的 float16，一起載入
v = (np.ones(EMBED_DIM) / np.sqrt(EMBED_DIM)).astype(np.float32)
kb2.conn.execute("INSERT INTO kb_chunks(pmid, section, ord, text, embedding) VALUES('1003','abstract',1,'extra',?)", (v.tobytes(),)); kb2.conn.commit(); kb2._matrix = None
assert kb2.nearest(v, k=1)[0]["sim"] > 0.99 and kb2._matrix.shape == (6, EMBED_DIM) and kb2._matrix.dtype == np.float32
kb2.close()
# 覆蓋保護 / --force / sha256 不符 / 本機找不到
try:
    K.import_pack(str(pack), dst=dst); raise AssertionError("應該拒絕覆蓋")
except RuntimeError as e:
    assert "--force" in str(e)
assert K.import_pack(str(pack), dst=dst, force=True)["stats"]["papers"] == 3
pack.with_name("knowledge.db.xz.sha256").write_text("0" * 64 + "  knowledge.db.xz\n")
try:
    K.import_pack(str(pack), dst=tmp / "k3.db"); raise AssertionError("sha256 不符應該失敗")
except RuntimeError as e:
    assert "sha256" in str(e) and not (tmp / "k3.db").exists()
try:
    K.import_pack(str(tmp / "nope.xz"), dst=tmp / "k4.db"); raise AssertionError()
except FileNotFoundError:
    pass
# 只有分片（單檔不在）：本機接回來；假網址：單檔 404 → 逐片抓到 404 為止
good_sha = K._sha256(pack); pack.with_name("knowledge.db.xz.sha256").write_text(f"{good_sha}  knowledge.db.xz\n")
single = pack.read_bytes(); pack.unlink()
assert K.import_pack(str(pack), dst=tmp / "k6.db")["stats"]["chunks"] == 5
import types, requests
served = {f"https://x/kb.xz.{i:03d}": p.read_bytes() for i, p in enumerate(parts, 1)}; served["https://x/kb.xz.sha256"] = f"{good_sha}  kb.xz\n".encode()
log_dl = []
def fake_download(url, dst):
    log_dl.append(url)
    if url not in served: raise requests.HTTPError(response=types.SimpleNamespace(status_code=404))
    Path(dst).write_bytes(served[url])
real_get = requests.get
requests.get = lambda url, **kw: types.SimpleNamespace(status_code=200, text=served[url].decode()) if url in served else types.SimpleNamespace(status_code=404, text="")
K._download = fake_download
r = K.import_pack("https://x/kb.xz", dst=tmp / "k7.db")
assert r["verified"] and r["stats"]["chunks"] == 5 and log_dl[0] == "https://x/kb.xz" and log_dl[-1] == f"https://x/kb.xz.{len(parts) + 1:03d}", (r, log_dl)
try:
    K.import_pack("https://x/none.xz", dst=tmp / "k8.db"); raise AssertionError("單檔與分片都 404 應該失敗")
except requests.HTTPError:
    pass
# 期望值順序：--sha256 參數 > 預設來源的 KB_PACK_SHA256 > 網址旁的 .sha256
try:
    K.import_pack("https://x/kb.xz", dst=tmp / "k9.db", sha256="f" * 64); raise AssertionError("參數 sha256 不符應該失敗")
except RuntimeError as e:
    assert "sha256" in str(e)
from garmin_coach import config
config.KB_PACK_URL, config.KB_PACK_SHA256 = "https://x/kb.xz", good_sha
assert K.import_pack(None, dst=tmp / "k10.db")["verified"]
config.KB_PACK_SHA256 = "e" * 64
try:
    K.import_pack(None, dst=tmp / "k11.db"); raise AssertionError("釘住的 sha256 不符應該失敗")
except RuntimeError:
    pass
# 預設來源 404 → 備援來源（仍用 KB_PACK_SHA256 驗）；非預設來源不備援
config.KB_PACK_URL, config.KB_PACK_FALLBACK_URL, config.KB_PACK_SHA256 = "https://x/none.xz", "https://x/kb.xz", good_sha
r = K.import_pack(None, dst=tmp / "k12.db")
assert r["verified"] and r["source"] == "https://x/kb.xz" and r["stats"]["chunks"] == 5, r
try:
    K.import_pack("https://x/none.xz", dst=tmp / "k13.db"); raise AssertionError("指定來源 404 不該備援")
except requests.HTTPError:
    pass
config.KB_PACK_FALLBACK_URL = ""
try:
    K.import_pack(None, dst=tmp / "k14.db"); raise AssertionError("沒有備援應該失敗")
except requests.HTTPError:
    pass
requests.get = real_get
pack.write_bytes(single)
assert K._expected_sha(str(tmp / "nope.xz"), None) is None
print("export/import ok")

# 3) 論文清單 → 照清單重建（假 PubMed / 假 Europe PMC）
mf = tmp / "papers.tsv"
assert K.write_manifest(kb, mf) == 3
rows = K.read_manifest(mf)
assert [r["pmid"] for r in rows] == ["1001", "1002", "1003"] and rows[0]["topics"] == ["interval", "zone2"] and rows[0]["evidence"] == "meta"
assert rows[1]["fulltext"] == "none" and rows[0]["fulltext"] == "ok" and rows[2]["fulltext"] == "" and rows[2]["pmcid"] == "" and rows[0]["pmcid"] == "PMC1001"
assert mf.read_text(encoding="utf-8").splitlines()[0] == "\t".join(K.MANIFEST_FIELDS)
calls = {"fetch": [], "epmc": []}
def fake_fetch(pmids):
    calls["fetch"].append(list(pmids)); return [p for p in PAPERS if p["pmid"] in pmids and p["pmid"] != "1003"]   # 1003 當作 PubMed 已刪
def fake_epmc(pmcid):
    calls["epmc"].append(pmcid); return SECTIONS if pmcid == "PMC1001" else None
K.pubmed_fetch, K.epmc_fulltext = fake_fetch, fake_epmc
kb3 = KnowledgeBase(tmp / "k5.db")
rep = K.build_from_manifest(kb3, mf, fulltext=True, embed=False)
assert rep["manifest"] == 3 and rep["added"] == 2 and rep["missing"] == 1 and rep["fulltext_ok"] == 1, rep
assert calls["fetch"] == [["1001", "1002", "1003"]] and calls["epmc"] == ["PMC1001"]          # 1002 標 none，沒再向 Europe PMC 試
assert json.loads(kb3.conn.execute("SELECT topics FROM kb_papers WHERE pmid='1001'").fetchone()[0]) == ["interval", "zone2"]
assert kb3.conn.execute("SELECT fulltext_status FROM kb_papers WHERE pmid='1002'").fetchone()[0] == "none"
st3 = kb3.stats(); assert st3["papers"] == 2 and st3["with_fulltext"] == 1 and st3["chunks"] == 4 and st3["embedded"] == 0, st3
# 重跑：已存在的略過，不再抓
rep2 = K.build_from_manifest(kb3, mf, fulltext=True, embed=False)
assert rep2["added"] == 0 and len(calls["fetch"]) == 2 and calls["fetch"][1] == ["1003"], (rep2, calls)
kb3.close(); kb.close()
print("manifest ok")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK (kb)")
