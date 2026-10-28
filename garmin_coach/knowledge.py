"""背景知識庫（RAG）：近 10 年運動科學論文 → 本地向量索引。

來源：
* PubMed E-utilities：標題、摘要、作者、期刊、年份、DOI、PMC id、文獻類型
* Europe PMC fullTextXML：開放全文（有 PMC id 且 OA 的論文）
索引：
* data/knowledge.db：kb_papers / kb_chunks（embedding BLOB）/ kb_fts（FTS5 BM25）
* 檢索 = 向量餘弦 + BM25，Reciprocal Rank Fusion 合併
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import requests

from . import config

log = logging.getLogger(__name__)

KB_DB = config.DATA_DIR / "knowledge.db"
KB_RAW = config.DATA_DIR / "knowledge_raw"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
CHUNK_CHARS = 2400
CHUNK_OVERLAP = 250
PUBMED_PAUSE = 0.4           # 無 API key：每秒最多 3 次
HQ_FILTER = ' AND (review[pt] OR meta-analysis[pt] OR "randomized controlled trial"[pt] OR "systematic review"[pt])'
BASE_FILTER = " AND humans[mh] AND english[la]"

# 主題 → PubMed 查詢
TOPICS: dict[str, dict[str, str]] = {
    "zone2": {"label": "Zone 2 / 低強度 / 強度分布",
              "query": '("zone 2" OR "low-intensity training" OR "polarized training" OR "pyramidal training" OR "training intensity distribution") AND (endurance OR runners OR cyclists OR athletes)'},
    "lt1_lt2": {"label": "LT1 / LT2 / 閾值訓練",
                "query": '("lactate threshold" OR "aerobic threshold" OR "anaerobic threshold" OR "ventilatory threshold" OR "maximal lactate steady state" OR "critical power" OR "critical speed") AND (training OR endurance OR runners OR cyclists)'},
    "interval": {"label": "間歇訓練",
                 "query": '("high-intensity interval training" OR "sprint interval training" OR "interval training") AND (endurance OR VO2max OR runners OR cyclists)'},
    "endurance": {"label": "耐力訓練",
                  "query": '("endurance training" OR "endurance performance") AND (adaptation OR periodization OR "training load" OR durability)'},
    "strength": {"label": "肌力訓練",
                 "query": '("strength training" OR "resistance training" OR plyometric) AND ("running economy" OR "endurance performance" OR runners OR cyclists)'},
    "running": {"label": "跑步訓練",
                "query": '("running economy" OR "running performance" OR "running injuries" OR "running cadence" OR "running biomechanics") AND runners'},
    "cycling": {"label": "自行車訓練",
                "query": '(cyclists OR "cycling performance" OR "functional threshold power" OR "cycling power") AND training'},
    "marathon": {"label": "馬拉松訓練",
                 "query": 'marathon AND (training OR pacing OR performance OR tapering OR "race prediction")'},
    "nutrition": {"label": "運動飲食",
                  "query": '("sports nutrition" OR "carbohydrate intake" OR "carbohydrate periodization" OR hydration OR caffeine OR "protein intake" OR "low energy availability") AND (endurance OR marathon OR runners OR cyclists)'},
    "recovery_monitoring": {"label": "恢復與監控（HRV、睡眠、負荷、熱適應、減量）",
                            "query": '("heart rate variability" OR "training readiness" OR "acute:chronic workload" OR "sleep" OR "heat acclimation" OR tapering) AND (endurance OR athletes OR runners OR cyclists) AND (training OR performance OR recovery)'},
    # ---- 運動醫學：傷害與預防（2026-10-20 加）----
    "injury_running": {"label": "跑步運動傷害（流行病學、危險因子、常見傷害）",
                       "query": '("running-related injury" OR "running-related injuries" OR "running injuries" OR "patellofemoral pain" OR "Achilles tendinopathy" OR "iliotibial band" OR "plantar fasciitis" OR "medial tibial stress syndrome" OR "bone stress injury" OR "stress fracture") AND (runners OR running)'},
    "injury_cycling": {"label": "自行車運動傷害（過度使用、bike fit、膝/腰/頸/會陰）",
                       "query": '(cyclists OR cycling OR bicycle) AND ("overuse injury" OR "overuse injuries" OR "bike fit" OR "bicycle fit" OR "knee pain" OR "low back pain" OR "neck pain" OR "saddle" OR "perineal" OR "hand numbness" OR "ulnar neuropathy" OR injuries)'},
    "injury_prevention": {"label": "傷害預防與回場（負荷管理、肌力、復健、肌腱/骨壓力傷害）",
                          "query": '("injury prevention" OR "injury risk" OR "return to sport" OR "return to running" OR rehabilitation OR "load management" OR "training load" OR "tendinopathy management" OR "bone stress injuries") AND (endurance OR runners OR cyclists OR triathletes OR "endurance athletes")'},
    "sports_medicine": {"label": "運動醫學（熱傷害、低血鈉、過度訓練/RED-S、心臟、感染）",
                        "query": '("exertional heat illness" OR "heat stroke" OR "exercise-associated hyponatremia" OR "overtraining syndrome" OR "relative energy deficiency" OR "low energy availability" OR "sudden cardiac" OR "athlete\'s heart" OR "upper respiratory" OR "exercise-induced bronchoconstriction" OR "gastrointestinal symptoms") AND (endurance OR marathon OR runners OR cyclists OR triathletes OR athletes)'},
}

TOPIC_BOOST = 1.25            # 指定主題的論文加分（不排除其他主題）


def pub_type_weight(pub_types: list[str]) -> float:
    """證據等級加權：統合分析/系統性回顧 > RCT/共識 > 一般回顧/其他。"""
    pts = {p.lower() for p in pub_types}
    if any("meta-analysis" in p or "systematic review" in p for p in pts):
        return 1.3
    if any("randomized controlled trial" in p or "consensus" in p or "guideline" in p for p in pts):
        return 1.15
    return 1.0


DDL = """
CREATE TABLE IF NOT EXISTS kb_papers (
    pmid TEXT PRIMARY KEY,
    title TEXT, abstract TEXT, authors TEXT, journal TEXT, year INTEGER, doi TEXT, pmcid TEXT,
    pub_types TEXT, mesh TEXT, topics TEXT,
    fulltext_status TEXT,              -- ok / none / error / null(未嘗試)
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pmid TEXT NOT NULL,
    section TEXT,
    ord INTEGER,
    text TEXT NOT NULL,
    embedding BLOB,
    FOREIGN KEY (pmid) REFERENCES kb_papers(pmid)
);
CREATE INDEX IF NOT EXISTS idx_chunks_pmid ON kb_chunks(pmid);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(text, content='kb_chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON kb_chunks BEGIN
    INSERT INTO kb_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON kb_chunks BEGIN
    INSERT INTO kb_fts(kb_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============ PubMed ============

def pubmed_search(query: str, retmax: int, since: int = 2016, until: int = 2026, hq: bool = True) -> list[str]:
    params = {
        "db": "pubmed", "term": query + (HQ_FILTER if hq else "") + BASE_FILTER,
        "mindate": str(since), "maxdate": str(until), "datetype": "pdat",
        "retmode": "json", "retmax": retmax, "sort": "relevance",
        "tool": "garmin_coach", "email": config.PUBMED_EMAIL,
    }
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    time.sleep(PUBMED_PAUSE)
    return r.json()["esearchresult"].get("idlist", [])


def _text(el: Optional[ET.Element]) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def pubmed_fetch(pmids: list[str]) -> list[dict[str, Any]]:
    """efetch 一批 PMID，解析成 dict。"""
    out: list[dict[str, Any]] = []
    for i in range(0, len(pmids), 100):
        batch = pmids[i:i + 100]
        r = requests.post(f"{EUTILS}/efetch.fcgi", data={"db": "pubmed", "id": ",".join(batch), "retmode": "xml",
                                                        "tool": "garmin_coach", "email": config.PUBMED_EMAIL}, timeout=120)
        r.raise_for_status()
        time.sleep(PUBMED_PAUSE)
        root = ET.fromstring(r.content)
        for art in root.findall(".//PubmedArticle"):
            mc = art.find("MedlineCitation")
            if mc is None:
                continue
            a = mc.find("Article")
            pmid = _text(mc.find("PMID"))
            abstract_parts = []
            for ab in (a.findall("Abstract/AbstractText") if a is not None else []):
                label = ab.get("Label")
                t = _text(ab)
                abstract_parts.append(f"{label}: {t}" if label else t)
            year = None
            for path in ("Journal/JournalIssue/PubDate/Year", "ArticleDate/Year"):
                y = _text(a.find(path)) if a is not None else ""
                if y.isdigit():
                    year = int(y); break
            if year is None and a is not None:
                md = _text(a.find("Journal/JournalIssue/PubDate/MedlineDate"))
                m = re.search(r"(20\d\d)", md)
                year = int(m.group(1)) if m else None
            authors = []
            for au in (a.findall("AuthorList/Author") if a is not None else []):
                ln, ini = _text(au.find("LastName")), _text(au.find("Initials"))
                if ln:
                    authors.append(f"{ln} {ini}".strip())
            ids = {aid.get("IdType"): _text(aid) for aid in art.findall("PubmedData/ArticleIdList/ArticleId")}
            out.append({
                "pmid": pmid, "title": _text(a.find("ArticleTitle")) if a is not None else "",
                "abstract": "\n".join(abstract_parts),
                "authors": authors, "journal": _text(a.find("Journal/Title")) if a is not None else "", "year": year,
                "doi": ids.get("doi"), "pmcid": ids.get("pmc"),
                "pub_types": [_text(pt) for pt in (a.findall("PublicationTypeList/PublicationType") if a is not None else [])],
                "mesh": [_text(mh.find("DescriptorName")) for mh in mc.findall("MeshHeadingList/MeshHeading")],
            })
    return out


# ============ Europe PMC 全文 ============

def epmc_fulltext(pmcid: str) -> Optional[list[tuple[str, str]]]:
    """回傳 [(section title, text), ...]；非開放取用回 None。"""
    pid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    r = requests.get(f"{EPMC}/{pid}/fullTextXML", timeout=60)
    if r.status_code == 404 or not r.content:
        return None
    r.raise_for_status()
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return None
    body = root.find(".//body")
    if body is None:
        return None
    sections: list[tuple[str, str]] = []

    def walk(sec: ET.Element, parent_title: str) -> None:
        title = _text(sec.find("title")) or parent_title
        paras = []
        for child in sec:
            if child.tag == "sec":
                continue
            if child.tag in ("p", "list", "table-wrap", "fig", "boxed-text", "disp-quote"):
                t = " ".join(_text(child).split())
                if len(t) > 40:
                    paras.append(t)
        if paras:
            sections.append((title, "\n".join(paras)))
        for sub in sec.findall("sec"):
            walk(sub, title)

    top = body.findall("sec")
    if top:
        for sec in top:
            walk(sec, "Body")
    else:
        t = " ".join(_text(body).split())
        if t:
            sections.append(("Body", t))
    # 去掉參考文獻 / 致謝類
    sections = [(t, x) for t, x in sections if not re.match(r"^(references?|acknowledg|funding|conflicts?|author contributions|supplementary)", t.strip(), re.I)]
    return sections or None


# ============ 切段 ============

def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(". ", start + size // 2, end))
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if len(c) > 80]


# ============ Embedding ============

class Embedder:
    def __init__(self, api_key: Optional[str] = None) -> None:
        from openai import OpenAI
        key = api_key or config.OPENAI_API_KEY
        if not key:
            raise RuntimeError("未設定 OPENAI_API_KEY")
        self.client = OpenAI(api_key=key)
        self.tokens = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for i in range(0, len(texts), 64):
            batch = [t[:8000] for t in texts[i:i + 64]]
            for attempt in range(4):
                try:
                    r = self.client.embeddings.create(model=EMBED_MODEL, input=batch)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 3:
                        raise
                    log.warning("embedding 重試 %s: %s", attempt + 1, e)
                    time.sleep(2 * (attempt + 1))
            self.tokens += getattr(r.usage, "total_tokens", 0) or 0
            for j, d in enumerate(r.data):
                out[i + j] = np.asarray(d.embedding, dtype=np.float32)
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return out / norms


# ============ 知識庫 ============

class KnowledgeBase:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or KB_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self._matrix: Optional[np.ndarray] = None
        self._ids: Optional[np.ndarray] = None

    def close(self) -> None:
        self.conn.close()

    # ---------- 建庫 ----------
    def add_papers(self, papers: Iterable[dict[str, Any]], topic: str) -> int:
        n = 0
        for p in papers:
            if not p.get("pmid") or not (p.get("abstract") or p.get("title")):
                continue
            row = self.conn.execute("SELECT topics FROM kb_papers WHERE pmid=?", (p["pmid"],)).fetchone()
            if row:
                topics = set(json.loads(row["topics"] or "[]")) | {topic}
                self.conn.execute("UPDATE kb_papers SET topics=? WHERE pmid=?", (json.dumps(sorted(topics)), p["pmid"]))
                continue
            self.conn.execute(
                "INSERT INTO kb_papers(pmid,title,abstract,authors,journal,year,doi,pmcid,pub_types,mesh,topics,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["pmid"], p["title"], p["abstract"], json.dumps(p["authors"], ensure_ascii=False), p["journal"], p["year"],
                 p.get("doi"), p.get("pmcid"), json.dumps(p["pub_types"]), json.dumps(p["mesh"]), json.dumps([topic]), _now()),
            )
            # 摘要段
            header = f"{p['title']}\n{p['journal']} {p['year'] or ''}\n"
            self.conn.execute("INSERT INTO kb_chunks(pmid, section, ord, text) VALUES(?,?,?,?)",
                              (p["pmid"], "abstract", 0, header + (p["abstract"] or "")))
            (KB_RAW / "papers").mkdir(parents=True, exist_ok=True)
            (KB_RAW / "papers" / f"{p['pmid']}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            n += 1
        self.conn.commit()
        return n

    def add_fulltext(self, pmid: str, sections: list[tuple[str, str]]) -> int:
        paper = self.conn.execute("SELECT title, journal, year FROM kb_papers WHERE pmid=?", (pmid,)).fetchone()
        header = f"{paper['title']} ({paper['journal']} {paper['year'] or ''})\n" if paper else ""
        n = 0
        for title, text in sections:
            for i, ch in enumerate(chunk_text(text)):
                self.conn.execute("INSERT INTO kb_chunks(pmid, section, ord, text) VALUES(?,?,?,?)",
                                  (pmid, title[:80], i, f"{header}[{title}] {ch}"))
                n += 1
        self.conn.execute("UPDATE kb_papers SET fulltext_status='ok' WHERE pmid=?", (pmid,))
        self.conn.commit()
        return n

    def mark_fulltext(self, pmid: str, status: str) -> None:
        self.conn.execute("UPDATE kb_papers SET fulltext_status=? WHERE pmid=?", (status, pmid))
        self.conn.commit()

    def embed_missing(self, embedder: Embedder, batch: int = 256) -> int:
        total = 0
        while True:
            rows = self.conn.execute("SELECT id, text FROM kb_chunks WHERE embedding IS NULL LIMIT ?", (batch,)).fetchall()
            if not rows:
                break
            vecs = embedder.embed([r["text"] for r in rows])
            self.conn.executemany("UPDATE kb_chunks SET embedding=? WHERE id=?",
                                  [(vecs[i].tobytes(), r["id"]) for i, r in enumerate(rows)])
            self.conn.commit()
            total += len(rows)
            log.info("embedded %d (累計 %d, tokens %d)", len(rows), total, embedder.tokens)
        self._matrix = None
        return total

    # ---------- 檢索 ----------
    def _load_matrix(self) -> None:
        rows = self.conn.execute("SELECT id, embedding FROM kb_chunks WHERE embedding IS NOT NULL").fetchall()
        self._ids = np.array([r["id"] for r in rows], dtype=np.int64)
        self._matrix = (np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                        if rows else np.zeros((0, EMBED_DIM), dtype=np.float32))

    def search(self, query: str, k: int = 6, topic: Optional[str] = None, embedder: Optional[Embedder] = None,
               year_from: Optional[int] = None, qvec: Optional[np.ndarray] = None) -> list[dict[str, Any]]:
        """qvec：已算好的查詢向量（省一次 embedding 呼叫）。"""
        if self._matrix is None:
            self._load_matrix()
        assert self._matrix is not None and self._ids is not None
        n_cand = max(30, k * 5)
        scores: dict[int, float] = {}
        # 向量
        if (embedder or qvec is not None) and len(self._ids):
            q = qvec if qvec is not None else embedder.embed([query])[0]   # type: ignore[union-attr]
            sims = self._matrix @ np.asarray(q, dtype=np.float32)
            top = np.argsort(-sims)[:n_cand]
            for rank, idx in enumerate(top):
                scores[int(self._ids[idx])] = scores.get(int(self._ids[idx]), 0) + 1 / (60 + rank)
        # BM25
        terms = [t for t in re.findall(r"[A-Za-z0-9\-]{3,}", query)]
        if terms:
            fts_q = " OR ".join(f'"{t}"' for t in terms[:12])
            try:
                rows = self.conn.execute("SELECT rowid FROM kb_fts WHERE kb_fts MATCH ? ORDER BY bm25(kb_fts) LIMIT ?", (fts_q, n_cand)).fetchall()
                for rank, r in enumerate(rows):
                    scores[int(r["rowid"])] = scores.get(int(r["rowid"]), 0) + 1 / (60 + rank)
            except sqlite3.OperationalError as e:
                log.debug("fts error: %s", e)
        if not scores:
            return []
        # 重新加權：topic 只是加分不是過濾（減量論文在 recovery_monitoring，用 endurance 過濾會只剩一篇大型綜述）；
        # 統合分析/系統性回顧/RCT/共識聲明優先於敘述性綜述
        cands = []
        for cid, sc in sorted(scores.items(), key=lambda x: -x[1])[: n_cand * 2]:
            r = self.conn.execute(
                "SELECT c.id, c.pmid, c.section, c.text, p.title, p.authors, p.journal, p.year, p.doi, p.pub_types, p.topics "
                "FROM kb_chunks c JOIN kb_papers p ON p.pmid=c.pmid WHERE c.id=?", (cid,)).fetchone()
            if not r:
                continue
            if year_from and (r["year"] or 0) < year_from:
                continue
            w = 1.0
            if topic and topic in json.loads(r["topics"] or "[]"):
                w *= TOPIC_BOOST
            w *= pub_type_weight(json.loads(r["pub_types"] or "[]"))
            cands.append((sc * w, r))
        cands.sort(key=lambda x: -x[0])
        out, seen_pmids = [], set()
        for sc, r in cands:
            if r["pmid"] in seen_pmids and len(out) < k:      # 同一篇最多一段，增加多樣性
                continue
            seen_pmids.add(r["pmid"])
            authors = json.loads(r["authors"] or "[]")
            out.append({
                "score": round(sc, 4), "pmid": r["pmid"], "doi": r["doi"], "title": r["title"],
                "citation": f"{authors[0].split()[0] if authors else '?'}{' et al.' if len(authors) > 1 else ''} ({r['year']}) {r['journal']}",
                "pub_types": json.loads(r["pub_types"] or "[]"), "section": r["section"], "text": r["text"],
            })
            if len(out) >= k:
                break
        return out

    def nearest(self, vec: np.ndarray, k: int = 3) -> list[dict[str, Any]]:
        """純向量最近鄰（餘弦），每篇只回一段。給 websearch 閘門判斷「知識庫有沒有這類東西」用。"""
        if self._matrix is None:
            self._load_matrix()
        assert self._matrix is not None and self._ids is not None
        if not len(self._ids):
            return []
        sims = self._matrix @ np.asarray(vec, dtype=np.float32)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx in np.argsort(-sims)[: k * 4]:
            r = self.conn.execute(
                "SELECT c.pmid, c.section, c.text, p.title, p.authors, p.journal, p.year, p.pub_types "
                "FROM kb_chunks c JOIN kb_papers p ON p.pmid=c.pmid WHERE c.id=?", (int(self._ids[idx]),)).fetchone()
            if not r or r["pmid"] in seen:
                continue
            seen.add(r["pmid"])
            authors = json.loads(r["authors"] or "[]")
            out.append({"sim": round(float(sims[idx]), 3), "pmid": r["pmid"], "title": r["title"], "year": r["year"],
                        "citation": f"{authors[0].split()[0] if authors else '?'}{' et al.' if len(authors) > 1 else ''} ({r['year']}) {r['journal']}",
                        "pub_types": json.loads(r["pub_types"] or "[]"), "section": r["section"], "text": r["text"]})
            if len(out) >= k:
                break
        return out

    # ---------- 統計 ----------
    def stats(self) -> dict[str, Any]:
        c = self.conn
        per_topic = {}
        for r in c.execute("SELECT topics FROM kb_papers"):
            for t in json.loads(r["topics"] or "[]"):
                per_topic[t] = per_topic.get(t, 0) + 1
        return {
            "papers": c.execute("SELECT COUNT(*) FROM kb_papers").fetchone()[0],
            "with_fulltext": c.execute("SELECT COUNT(*) FROM kb_papers WHERE fulltext_status='ok'").fetchone()[0],
            "chunks": c.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0],
            "embedded": c.execute("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL").fetchone()[0],
            "years": dict(c.execute("SELECT year, COUNT(*) FROM kb_papers GROUP BY year ORDER BY year").fetchall()),
            "pub_types": dict(c.execute(
                "SELECT CASE WHEN pub_types LIKE '%Meta-Analysis%' THEN 'meta' WHEN pub_types LIKE '%Systematic Review%' THEN 'sysrev' "
                "WHEN pub_types LIKE '%Randomized%' THEN 'rct' WHEN pub_types LIKE '%Review%' THEN 'review' ELSE 'other' END, COUNT(*) "
                "FROM kb_papers GROUP BY 1").fetchall()),
            "per_topic": per_topic,
        }


# ============ 建庫流程 ============

def build(kb: KnowledgeBase, topics: Optional[list[str]] = None, per_topic_hq: int = 120, per_topic_general: int = 60,
          since: int = 2016, fulltext: bool = True, embed: bool = True, retry_errors: bool = False) -> dict[str, Any]:
    KB_RAW.mkdir(parents=True, exist_ok=True)
    topics = list(TOPICS) if topics is None else topics     # 傳 [] = 不重新搜尋，只補全文/embedding
    report: dict[str, Any] = {}
    for t in topics:
        spec = TOPICS[t]
        ids = pubmed_search(spec["query"], per_topic_hq, since, hq=True)
        ids += [i for i in pubmed_search(spec["query"], per_topic_general, since, hq=False) if i not in ids]
        existing = {r["pmid"] for r in kb.conn.execute("SELECT pmid FROM kb_papers")}
        new_ids = [i for i in ids if i not in existing]
        papers = pubmed_fetch(new_ids) if new_ids else []
        added = kb.add_papers(papers, t)
        # 已存在的也要掛上主題
        for pid in ids:
            if pid in existing:
                kb.add_papers([{"pmid": pid, "title": "x", "abstract": "x", "authors": [], "journal": "", "year": None, "pub_types": [], "mesh": []}], t)
        report[t] = {"found": len(ids), "added": added}
        log.info("topic %s: %d 篇（新增 %d）", t, len(ids), added)

    if fulltext:
        cond = "fulltext_status IS NULL OR fulltext_status='error'" if retry_errors else "fulltext_status IS NULL"
        rows = kb.conn.execute(f"SELECT pmid, pmcid FROM kb_papers WHERE pmcid IS NOT NULL AND pmcid != '' AND ({cond})").fetchall()
        log.info("嘗試抓全文：%d 篇有 PMC id", len(rows))
        ok = 0
        for i, r in enumerate(rows):
            try:
                secs = epmc_fulltext(r["pmcid"])
            except Exception as e:  # noqa: BLE001
                log.warning("fulltext %s 失敗: %s", r["pmcid"], e)
                kb.mark_fulltext(r["pmid"], "error")
                time.sleep(1)
                continue
            if secs:
                n = kb.add_fulltext(r["pmid"], secs)
                (KB_RAW / "fulltext").mkdir(parents=True, exist_ok=True)
                (KB_RAW / "fulltext" / f"{r['pmid']}.json").write_text(json.dumps(secs, ensure_ascii=False), encoding="utf-8")
                ok += 1
            else:
                kb.mark_fulltext(r["pmid"], "none")
            if (i + 1) % 50 == 0:
                log.info("fulltext 進度 %d/%d（成功 %d）", i + 1, len(rows), ok)
            time.sleep(0.25)
        report["fulltext_ok"] = ok
    if embed:
        emb = Embedder()
        report["embedded"] = kb.embed_missing(emb)
        report["embed_tokens"] = emb.tokens
    report["stats"] = kb.stats()
    return report
