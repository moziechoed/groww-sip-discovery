"""Golden set sampler: 0 LLM calls, blind template, never overwrites labels, guide matches the prompt."""
import csv
import json
from pathlib import Path

from core.config import ROOT
from pipeline import golden as G


def mk(i, source, text, borderline=False, hinglish=False):
    return {"id": f"{source}:{i}", "source": source, "text": text, "clean_text": text,
            "clean_borderline": borderline, "hinglish": hinglish}


STORIES = [
    "I started my first SIP last year and I stopped it in month five because my autopay mandate failed twice.",
    "I have been investing for two years and I kept my SIP running even when returns were negative this year.",
    "Maine SIP band kar diya kyunki mandate fail ho gaya aur paisa kat gaya, mera sip ab paused hai bhai yaar.",
    "I restarted my SIP after a pause of four months, I had paused it because of a medical expense at home.",
    "I reduced my SIP amount from 5000 to 2000 because my salary got delayed and I was continuing anyway.",
]


def settings_for(make_settings, tmp_path, posts):
    s = make_settings()
    s.paths.seeds_dir = str(tmp_path / "seeds")
    s.paths.outputs = str(tmp_path / "outputs")
    clean = Path(s.paths.clean_dir)
    clean.mkdir(parents=True, exist_ok=True)
    with open(clean / "clean_posts.jsonl", "w", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(p) + "\n")
    return s


def corpus():
    posts = []
    for src in ["youtube", "reddit", "play_store", "tradingqna", "app_store"]:
        for i in range(12):
            posts.append(mk(i, src, STORIES[i % 5] + f" ({src} {i})", borderline=(i == 11), hinglish=(i % 5 == 2)))
    return posts


def test_selects_12_stratified_8_ambiguous_deterministically(make_settings, tmp_path):
    s = settings_for(make_settings, tmp_path, corpus())
    a = G.select_golden(corpus(), s)
    b = G.select_golden(corpus(), s)
    assert [p["id"] for p, _ in a] == [p["id"] for p, _ in b]
    buckets = [bk for _, bk in a]
    assert buckets.count("stratified") == 12 and buckets.count("ambiguous") == 8
    assert len({p["id"] for p, _ in a}) == 20


def test_writes_blind_template_and_never_overwrites_labels(make_settings, tmp_path):
    s = settings_for(make_settings, tmp_path, corpus())
    guide = tmp_path / "guide.md"
    orig = G._paths
    G._paths = lambda st: {**orig(st), "guide": guide}
    try:
        assert G.run_golden_sample(s) == 0
        human = Path(s.paths.seeds_dir) / "golden_human.csv"
        rows = list(csv.DictReader(open(human, encoding="utf-8")))
        assert len(rows) == 20 and list(rows[0].keys()) == G.CSV_COLUMNS
        assert all(not r[c] for r in rows for c in G.LABEL_COLUMNS)       # blank = blind
        complete, missing, total = G.labels_status(s)
        assert not complete and missing == 20 and total == 20
        rows[0]["is_relevant"] = "false"
        with open(human, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=G.CSV_COLUMNS); w.writeheader(); w.writerows(rows)
        before = human.read_text(encoding="utf-8")
        assert G.run_golden_sample(s) == 0
        assert human.read_text(encoding="utf-8") == before                 # labels preserved
        assert G.golden_ids(s) == {r["post_id"] for r in rows}
    finally:
        G._paths = orig


def test_labels_status_requires_all_required_fields_for_relevant_rows(make_settings, tmp_path):
    s = settings_for(make_settings, tmp_path, corpus())
    human = Path(s.paths.seeds_dir) / "golden_human.csv"
    human.parent.mkdir(parents=True, exist_ok=True)
    full = {c: "x" for c in G.REQUIRED_LABELS}
    rows = [dict(post_id="a", is_relevant="false"), dict(post_id="b", **{**full, "is_relevant": "true"}),
            dict(post_id="c", is_relevant="true", outcome="stopped")]
    with open(human, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=G.CSV_COLUMNS); w.writeheader()
        for r in rows:
            w.writerow(r)
    complete, missing, total = G.labels_status(s)
    assert (complete, missing, total) == (False, 1, 3)


def test_guide_definitions_are_verbatim_from_prompt():
    prompt = (ROOT / "prompts" / "enrich_v1_20261027.txt").read_text(encoding="utf-8")
    defs = G.extract_definitions(prompt)
    guide = G.build_guide(prompt, "enrich_v1_20261027.txt")
    assert defs in guide and "kpi_branch" in defs and "17 moved to another platform" in defs
