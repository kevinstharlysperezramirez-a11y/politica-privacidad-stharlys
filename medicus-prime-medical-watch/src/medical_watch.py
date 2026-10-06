from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

try:
    from google import genai
except Exception:
    genai = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)
ROTATION = STATE / "rotation.json"

NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
HEADERS = {"User-Agent": "MEDICUS-PRIME/1.0"}

@dataclass
class Article:
    title: str
    authors: str
    journal: str
    pub_date: str
    pmid: str = ""
    doi: str = ""
    abstract: str = ""
    publication_types: str = ""
    source: str = ""
    url: str = ""
    topic: str = ""

    @property
    def key(self) -> str:
        if self.pmid:
            return "pmid:" + self.pmid
        if self.doi:
            return "doi:" + self.doi.lower()
        return "title:" + normalize(self.title)

def normalize(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9 ]+", " ", value or "").lower()
    return re.sub(r"\s+", " ", value).strip()

def date_ago(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()

def pubmed_ids(term: str, days: int) -> list[str]:
    query = f"({term}) AND ({date_ago(days)}[PDAT] : {dt.date.today().isoformat()}[PDAT])"
    r = requests.get(
        f"{NCBI}/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmode": "json",
                "retmax": CONFIG["project"]["max_articles_per_topic"], "sort": "pub date"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json()["esearchresult"]["idlist"]

def pubmed_fetch(ids: list[str], topic: str) -> list[Article]:
    if not ids:
        return []
    r = requests.get(
        f"{NCBI}/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        headers=HEADERS, timeout=45,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    output = []
    for node in root.findall(".//PubmedArticle"):
        pmid = node.findtext(".//PMID", default="").strip()
        title_node = node.find(".//ArticleTitle")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        abstracts = []
        for item in node.findall(".//Abstract/AbstractText"):
            text = "".join(item.itertext()).strip()
            label = item.attrib.get("Label")
            abstracts.append(f"{label}: {text}" if label else text)
        authors = []
        for author in node.findall(".//AuthorList/Author")[:8]:
            collective = author.findtext("CollectiveName")
            if collective:
                authors.append(collective)
            else:
                last = author.findtext("LastName", "")
                initials = author.findtext("Initials", "")
                name = f"{last} {initials}".strip()
                if name:
                    authors.append(name)
        doi = ""
        for aid in node.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi":
                doi = (aid.text or "").strip()
                break
        pubtypes = [x.text.strip() for x in node.findall(".//PublicationType") if x.text]
        year = node.findtext(".//PubDate/Year", default="")
        medline = node.findtext(".//PubDate/MedlineDate", default="")
        pub_date = year or medline
        output.append(Article(
            title=html.unescape(title),
            authors=", ".join(authors),
            journal=node.findtext(".//Journal/Title", default=""),
            pub_date=pub_date,
            pmid=pmid,
            doi=doi,
            abstract=" ".join(abstracts),
            publication_types=", ".join(pubtypes),
            source="PubMed",
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            topic=topic,
        ))
    return output

def epmc(term: str, days: int, topic: str) -> list[Article]:
    query = f'({term}) AND FIRST_PDATE:[{date_ago(days)} TO {dt.date.today().isoformat()}]'
    r = requests.get(
        f"{EPMC}/search",
        params={"query": query, "format": "json",
                "pageSize": CONFIG["project"]["max_articles_per_topic"],
                "sort": "FIRST_PDATE_D desc"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    output = []
    for x in r.json().get("resultList", {}).get("result", []):
        pmid = str(x.get("pmid") or "")
        doi = x.get("doi") or ""
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else "")
        output.append(Article(
            title=html.unescape(x.get("title") or ""),
            authors=x.get("authorString") or "",
            journal=x.get("journalTitle") or "",
            pub_date=x.get("firstPublicationDate") or str(x.get("pubYear") or ""),
            pmid=pmid,
            doi=doi,
            abstract=re.sub(r"\s+", " ", x.get("abstractText") or "").strip(),
            publication_types=", ".join(x.get("pubTypeList", {}).get("pubType", []) or []),
            source="Europe PMC",
            url=url,
            topic=topic,
        ))
    return output

def study_score(article: Article) -> float:
    text = f"{article.publication_types} {article.title}".lower()
    for pattern, score in [
        (r"meta-analysis|systematic review", 1.00),
        (r"randomized controlled trial|clinical trial", 0.95),
        (r"guideline|consensus", 0.90),
        (r"cohort|prospective|longitudinal", 0.78),
        (r"case-control", 0.72),
        (r"review", 0.60),
        (r"preprint", 0.35),
    ]:
        if re.search(pattern, text):
            return score
    return 0.50

def novelty_score(article: Article) -> float:
    text = f"{article.title} {article.abstract}".lower()
    signals = [
        "first-in-human", "phase 3", "phase iii", "phase 2", "phase ii",
        "novel", "new target", "breakthrough", "gene editing", "crispr",
        "car-t", "new biomarker", "early detection", "artificial intelligence",
        "machine learning", "precision medicine", "regenerative", "stem cell",
    ]
    return min(1.0, 0.45 + 0.06 * sum(s in text for s in signals))

def recency_score(pub_date: str) -> float:
    try:
        year = int(pub_date[:4])
    except Exception:
        return 0.25
    age = max(0, dt.date.today().year - year)
    return max(0.0, 1 - min(age, 5) / 5)

def rank(items: list[Article]) -> list[Article]:
    return sorted(items, key=lambda a: .45 * recency_score(a.pub_date) +
                  .35 * study_score(a) + .20 * novelty_score(a), reverse=True)

def selected_topics() -> list[dict[str, Any]]:
    topics = CONFIG["topic_rotation"]
    try:
        index = json.loads(ROTATION.read_text(encoding="utf-8")).get("index", 0)
    except Exception:
        index = 0
    batch = min(5, len(topics))
    chosen = [topics[(index + i) % len(topics)] for i in range(batch)]
    ROTATION.write_text(json.dumps({"index": (index + batch) % len(topics)}, indent=2), encoding="utf-8")
    return chosen

def collect() -> list[Article]:
    found: dict[str, Article] = {}
    days = CONFIG["project"]["recent_days"]
    fallback = CONFIG["project"]["fallback_days"]
    for topic in selected_topics():
        for window in (days, fallback):
            try:
                if CONFIG["sources"].get("pubmed", True):
                    ids = pubmed_ids(topic["query"], window)
                    for article in pubmed_fetch(ids, topic["name"]):
                        found.setdefault(article.key, article)
                if CONFIG["sources"].get("europe_pmc", True):
                    for article in epmc(topic["query"], window, topic["name"]):
                        found.setdefault(article.key, article)
            except requests.RequestException as exc:
                print(f"WARN {topic['name']}: {exc}")
            time.sleep(0.25)
            if any(a.topic == topic["name"] for a in found.values()):
                break
    return rank(list(found.values()))

def curate(articles: list[Article]) -> str | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key or genai is None or not articles:
        return None
    model = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
    client = genai.Client(api_key=key)
    data = [asdict(a) for a in articles[:CONFIG["project"]["max_final_articles"]]]
    prompt = """Eres el motor de curación científica de MEDICUS PRIME.
Usa exclusivamente los artículos proporcionados. No inventes ningún dato.
Selecciona y explica los hallazgos más relevantes para un estudiante de medicina.
Diferencia evidencia consolidada, prometedora, emergente, preclínica y experimental.
No confundas asociación con causalidad, significación estadística con importancia clínica, ni estudios animales con eficacia en humanos.
Devuelve Markdown en español, técnicamente riguroso, con:
# MEDICUS PRIME — ACTUALIZACIÓN CIENTÍFICA
## TOP 5
## OTROS HALLAZGOS RELEVANTES
## QUÉ DEBERÍA ESTUDIAR
## ALERTAS DE EVIDENCIA
Incluye título, fecha, revista, tipo de estudio, hallazgo, importancia, limitación y enlace."""
    response = client.models.generate_content(
        model=model,
        contents=prompt + "\n\nARTÍCULOS:\n" + json.dumps(data, ensure_ascii=False),
    )
    return getattr(response, "text", None)

def write_output(articles: list[Article], curated: str | None) -> None:
    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="minutes")
    lines = [
        "# MEDICUS PRIME — Medical Intelligence Feed", "",
        f"> Actualizado: **{now}**",
        "> Salida de vigilancia bibliográfica automatizada. Verifica siempre el artículo original antes de utilizar la evidencia clínicamente.",
        ""
    ]
    if curated:
        lines += [curated.strip(), ""]
    lines += ["## ARTÍCULOS DETECTADOS", ""]
    for i, a in enumerate(articles[:CONFIG["project"]["max_final_articles"]], 1):
        lines += [
            f"### {i}. {a.title}",
            f"**Área:** {a.topic}",
            f"**Fecha:** {a.pub_date}",
            f"**Revista:** {a.journal}",
            f"**Tipo:** {a.publication_types or 'No especificado'}",
            f"**Autores:** {a.authors or 'No disponible'}",
            f"**PMID:** {a.pmid or '—'}  **DOI:** {a.doi or '—'}",
            f"**Fuente:** {a.source}",
            f"**Enlace:** {a.url or '—'}", ""
        ]
    out = ROOT / CONFIG["project"]["output_file"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / "knowledge" / "medical_update.json").write_text(
        json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def main() -> None:
    articles = collect()
    curated = None
    try:
        curated = curate(articles)
    except Exception as exc:
        print(f"WARN Gemini curation unavailable: {exc}")
    write_output(articles, curated)
    print(f"MEDICUS PRIME: {len(articles)} artículos procesados.")

if __name__ == "__main__":
    main()
