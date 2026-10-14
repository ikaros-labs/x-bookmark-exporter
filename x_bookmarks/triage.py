"""Offline triage of exported bookmark notes into topic folders.

Builds a reviewable Markdown plan from keyword rules, authors already filed in
existing topic folders, and optional local TF-IDF similarity. Applying a plan
only ever touches a copy of the notes folder, never the source vault.
"""
import json
import math
import os
import re
import shutil
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_write

PLAN_MARKER = "x-bookmarks triage plan v1"
KEEP, DELETE = "keep", "delete"
DELETE_FOLDER = "_delete"
RESERVED = {"assets", ".x-bookmarks", ".obsidian", DELETE_FOLDER}
MIN_SCORE = 3.0

# Topic rules: strong keywords score 3, weak ones 1, author hints 2, all times
# the topic's boost (niche topics beat broad ones like ai/dev on a shared hit).
# A trailing * makes a prefix match, for inflected Russian words. Later entries
# win ties, so the more specific topics come last.
DEFAULT_RULES = {
    "health": {
        "strong": ["sleep tracker", "sleep trackers", "co2", "alcohol", "алкогол*", "workout", "fitness",
                   "nutrition", "сон", "здоровь*"],
        "weak": ["sleep", "health", "diet", "exercise", "meter"],
        "authors": [],
    },
    "productivity": {
        "strong": ["obsidian", "anytype", "notion", "second brain", "time tracking", "pkm", "note-taking",
                   "attention", "habit", "habits"],
        "weak": ["focus", "notes", "productivity", "workflow", "calendar", "todo"],
        "authors": [],
    },
    "learning": {
        "strong": ["3blue1brown", "game theory", "economists", "math", "mathematics", "physics"],
        "weak": ["book", "paper", "papers", "blog", "read", "reading", "learn", "learning", "video",
                 "explained", "лекци*", "книг*", "видео", "стать*", "изучени*"],
        "authors": ["Rainmaker1973"],
    },
    "startups": {
        "strong": ["startup", "startups", "founder", "founders", "saas", "b2c", "b2b", "cap table",
                   "make money", "money online", "стартап*", "нанима*"],
        "weak": ["company", "revenue", "customers", "hiring", "paid", "salary", "marketing", "business",
                 "launch", "memo"],
        "authors": ["levelsio", "marclou", "johnrush", "zfellows", "theweb3jess", "dagorenouf"],
    },
    "dev": {
        "strong": ["postgres", "postgresql", "postgresqlforeverything", "sql", "database", "databases",
                   "kubernetes", "k8s", "grafana", "prometheus", "opentelemetry", "observability",
                   "docker", "vps", "hetzner", "self-hosted", "selfhosted", "devops", "service mesh",
                   "service meshes", "outage", "status page", "terminal", "tmux", "cmux", "termius",
                   "git", "commit", "2fa", "linux", "nix", "dashboards", "backend", "cloudflare",
                   "hosting", "self-hosting", "encryption", "шифровани*", "разработ*", "программир*"],
        "weak": ["github", "open source", "open-source", "engineering", "engineer", "engineers",
                 "developer", "developers", "code", "coding", "api", "cloud", "server", "security",
                 "programming", "software", "repo", "dedicated", "разработчик", "код"],
        "authors": ["grafana", "iavins", "learnk8s", "K8sArchitect", "hnasr", "mitsuhiko", "Vlad_GitHub",
                    "unclebobmartin", "JustJake", "HSVSphere", "andrey_sitnik"],
    },
    "ai": {
        "strong": ["ai", "llm", "llms", "gpt", "chatgpt", "openai", "anthropic", "claude", "claude code",
                   "sonnet", "opus", "gemini", "grok", "cursor", "prompt", "prompts", "prompting",
                   "system prompt", "agent", "agents", "subagent", "subagents", "mcp", "codex", "devin",
                   "cline", "windsurf", "vibe coding", "agents.md", "claude.md", "openrouter",
                   "hugging face", "machine learning", "ml", "rag", "embeddings", "fable", "copilot",
                   "harness engineering", "agent loops", "ии", "нейросет*", "llm-*"],
        "weak": ["model", "models", "skill", "skills", "loop", "loops", "memory", "benchmark",
                 "dataset", "inference", "tokens", "automation"],
        "authors": ["karpathy", "rohanpaul_ai", "cognition", "cline", "_catwu", "Hesamation", "skirano",
                    "mckaywrigley", "_avichawla", "elder_plinius", "LiorOnAI", "kimmonismus", "swyx",
                    "mattpocockuk", "steipete", "akshay_pachaar", "jobergum", "micahomnd"],
    },
    "electronics": {
        "strong": ["pcb", "pcbs", "kicad", "esp32", "arduino", "raspberry pi", "microcontroller",
                   "solder", "soldering", "macropad", "jlcpcb", "flipper", "flipper zero", "iot",
                   "robotics", "robot", "robots", "animatronic", "actuator", "actuators", "faders",
                   "hardware", "sensor", "sensors", "circuit", "плата", "платы", "плату", "плате",
                   "платой", "платами", "печатная плата", "паяльн*"],
        "weak": ["diy", "makers", "maker", "prototype", "device", "smart lights", "console", "nfc"],
        "boost": 1.3,
        "authors": ["adafruit", "ctorobotics", "tomstokes", "IlirAliu_", "SamuelBeek", "Alacritic_Super"],
    },
    "3d-printing": {
        "strong": ["3d printing", "3d print", "3d-print", "3d printed", "3d-printed", "3d-printable",
                   "3d printer", "printables", "slicer", "slicing", "filament", "cad", "text-to-cad",
                   "plasticity", "stl", "bambu", "prusa", "parametric"],
        "weak": ["3d", "print", "printed", "design", "designer", "parts"],
        "boost": 1.5,
        "authors": ["printablescom", "womp3D", "getPlasticity", "Loftsmithcom", "earthtojake"],
    },
    "eink": {
        "strong": ["e-ink", "eink", "e-paper", "epaper", "e-reader", "ereader", "kindle", "киндл",
                   "daylightco", "minimal phone", "boox", "remarkable", "paper dev kit", "e ink"],
        "weak": ["display", "screen", "reader", "60hz", "60fps"],
        "boost": 2.0,
        "authors": ["hturan", "sunbelife", "Modostech"],
    },
}

STOPWORDS = set("""
the and for that this with you your are was were have has had not but all any can our out who
how what why when where which will would just they them their there then than from into about
more most some such been being its it's i'm i've im ive dont don't also very much many one two
get got make made like really here over only even still now new use used using via http https
com www status twitter x.com post posts amp let lets yes good great best thing things
это что как для все или его она они так уже было есть если только чтобы когда очень нет
""".split())

URL_RE = re.compile(r"https?://\S+")
EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]|!\[[^\]]*\]\(([^)]+)\)")
WORD_RE = re.compile(r"[^\W_][\w'.+-]*[^\W_]|[^\W_]", re.UNICODE)


class Note:
    def __init__(self, path, folder):
        self.path = path
        self.name = path.name
        self.folder = folder
        raw = path.read_text(encoding="utf-8", errors="replace")
        self.meta, body = parse_frontmatter(raw)
        self.author = str(self.meta.get("author") or "")
        self.post_id = str(self.meta.get("post_id") or "")
        self.media = [a or b for a, b in EMBED_RE.findall(body)]
        lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("[Original post](") or stripped.startswith("> Partial export"):
                continue
            if re.match(r"^(quoted|replied_to|retweeted): https://x\.com/", stripped):
                continue
            lines.append(line)
        text = EMBED_RE.sub(" ", "\n".join(lines))
        self.domains = [d.lower() for d in re.findall(r"https?://(?:www\.)?([^/\s)]+)", text)]
        # Outbound links are content: a "link:" bookmark is worth its URL. Their path words
        # (e.g. what_i_wish_i_knew_about_postgres) feed the keyword search.
        self.links = [u.rstrip(").,") for u in URL_RE.findall(text)
                      if not re.match(r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com|t\.co)/", u)]
        link_words = " ".join(re.sub(r"[/_\-.?=&#%+:]+", " ", re.sub(r"^https?://(?:www\.)?", "", u))
                              for u in self.links)
        self.text = " ".join(URL_RE.sub(" ", text).split())
        self.unavailable = "text unavailable" in self.text.lower() or "body unavailable" in self.text.lower()
        title = str(self.meta.get("article_title") or "")
        self.search = f"{title} {self.text} {' '.join(self.domains)} {link_words}".lower()

    def content_length(self):
        text = re.sub(r"@\w+", " ", self.text)
        return len(" ".join(re.sub(r"[^\w\s]", " ", text).split()))


def parse_frontmatter(raw):
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---", 4)
    if end < 0:
        return {}, raw
    meta = {}
    for line in raw[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value)
        except ValueError:
            meta[key.strip()] = value.strip("'\"")
    return meta, raw[end + 4:].lstrip("-\n")


def load_rules(path=None):
    rules = {topic: {k: (list(v) if isinstance(v, list) else v) for k, v in spec.items()}
             for topic, spec in DEFAULT_RULES.items()}
    if path is None:
        return rules
    data = json.loads(Path(path).read_text())
    if data.get("replace"):
        rules = {}
    for topic, spec in data.get("topics", {}).items():
        validate_folder(topic)
        merged = rules.setdefault(topic, {"strong": [], "weak": [], "authors": []})
        for key in ("strong", "weak", "authors"):
            merged.setdefault(key, [])
            merged[key] += [str(v) for v in spec.get(key, [])]
        if "boost" in spec:
            merged["boost"] = float(spec["boost"])
    return rules


def validate_folder(name):
    if (not name or name in {KEEP, DELETE} or name.lower() in RESERVED or name.startswith(".")
            or any(c in name for c in '/\\`[]\n\r') or name in {"..", "."}):
        raise ValueError(f"Invalid topic folder name: {name!r}")
    return name


def keyword_pattern(word):
    word = word.lower()
    if word.endswith("*"):
        return re.compile(r"(?<![\w-])" + re.escape(word[:-1]) + r"[\w-]*")
    return re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])")


def topic_folders(notes_root, inbox):
    folders = {}
    for child in sorted(notes_root.iterdir()):
        if (child.is_dir() and child.resolve() != inbox.resolve() and not child.name.startswith(".")
                and child.name.lower() not in RESERVED):
            folders[child.name] = [Note(p, child.name) for p in sorted(child.glob("*.md"))]
    return folders


def load_inbox(inbox):
    return [Note(p, inbox.name) for p in sorted(inbox.glob("*.md"))]


def score_note(note, rules, compiled, seed_authors):
    scores, reasons = {}, {}
    author = note.author.lower()
    for topic, spec in rules.items():
        score, why, anchored = 0.0, [], False
        for weight, key in ((3.0, "strong"), (1.0, "weak")):
            for word, pattern in compiled[topic][key]:
                hits = len(pattern.findall(note.search))
                if hits:
                    score += weight * min(hits, 2)
                    anchored = anchored or key == "strong"
                    why.append(word if hits == 1 else f"{word}×{hits}")
        if author and author in {a.lower() for a in spec.get("authors", [])}:
            score += 2.0
            why.append("author hint")
        if author and author in seed_authors.get(topic, set()):
            score += 4.0
            why.append(f"author already in {topic}/")
        # Weak keywords alone never file a note; they only add weight to a strong hit or author.
        if score and (anchored or "author" in " ".join(why)):
            scores[topic], reasons[topic] = score * float(spec.get("boost", 1.0)), why
    for topic, authors in seed_authors.items():
        if topic not in rules and author and author in authors:
            scores[topic] = scores.get(topic, 0) + 4.0
            reasons.setdefault(topic, []).append(f"author already in {topic}/")
    return scores, reasons


def tokens(text):
    out = []
    for word in WORD_RE.findall(text.lower()):
        word = word.strip(".'+-")
        if len(word) >= 3 and word not in STOPWORDS and not word.isdigit():
            out.append(word)
    return out


def tfidf_vectors(docs):
    counts = [Counter(tokens(d)) for d in docs]
    df = Counter(t for c in counts for t in c)
    n = len(docs)
    vectors = []
    for c in counts:
        vec = {t: (1 + math.log(f)) * math.log((1 + n) / (1 + df[t])) for t, f in c.items() if df[t] > 1}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def centroid(vectors):
    total = Counter()
    for vec in vectors:
        total.update(vec)
    norm = math.sqrt(sum(v * v for v in total.values())) or 1.0
    return {t: v / norm for t, v in total.items()}


def build_plan(vault, inbox_rel, rules=None, use_tfidf=False, tfidf_min=0.2, min_score=MIN_SCORE):
    """Return (entries, context) describing a proposed action per inbox note."""
    vault = Path(vault).expanduser().resolve()
    inbox = (vault / inbox_rel).resolve()
    if not inbox.is_relative_to(vault) or inbox == vault:
        raise ValueError("--inbox must be a folder inside the vault")
    if not inbox.is_dir():
        raise ValueError(f"Inbox folder not found: {inbox}")
    rules = rules if rules is not None else load_rules()
    notes_root = inbox.parent
    existing = topic_folders(notes_root, inbox)
    notes = load_inbox(inbox)
    compiled = {topic: {key: [(w, keyword_pattern(w)) for w in spec.get(key, [])] for key in ("strong", "weak")}
                for topic, spec in rules.items()}
    seed_authors = {topic: {n.author.lower() for n in folder_notes if n.author}
                    for topic, folder_notes in existing.items()}
    order = list(rules) + [t for t in existing if t not in rules]

    entries, seen_ids, seen_text = [], {}, {}
    for note in notes:
        scores, reasons = score_note(note, rules, compiled, seed_authors)
        best = max(scores, key=lambda t: (scores[t], order.index(t) if t in order else -1), default=None)
        entry = {"file": note.name, "author": note.author, "action": KEEP, "checked": True,
                 "score": 0.0, "why": [], "flags": [], "note": note}
        if best is not None and scores[best] >= min_score:
            entry.update(action=best, score=scores[best], why=reasons[best][:6])
            runner = sorted((s for t, s in scores.items() if t != best), reverse=True)
            if runner and runner[0] >= scores[best] - 1:
                entry["flags"].append(f"close call vs {next(t for t in scores if t != best and scores[t] == runner[0])}")
        dup_key = " ".join(note.text.lower().split())
        if note.post_id and note.post_id in seen_ids:
            entry["flags"].append(f"duplicate post of {seen_ids[note.post_id]}")
        elif len(dup_key) > 30 and dup_key in seen_text:
            entry["flags"].append(f"same text as {seen_text[dup_key]}")
        seen_ids.setdefault(note.post_id, note.name)
        seen_text.setdefault(dup_key, note.name)
        length = note.content_length()
        if note.links and length < 25 and not note.unavailable:
            entry["flags"].append(f"link only ({urllib.parse.urlsplit(note.links[0]).hostname})")
        elif note.unavailable:
            entry["flags"].append("post text unavailable (deleted/protected?)")
        elif length < 25 and not note.media:
            entry["flags"].append(f"almost no text ({length} chars)")
        elif length < 25:
            entry["flags"].append("media only, no text")
        elif note.text.startswith("@") and length < 90:
            entry["flags"].append("reply fragment")
        hard = any(f.startswith(("duplicate", "same text", "post text unavailable")) for f in entry["flags"])
        soft = any(f.startswith(("almost no text", "media only", "reply fragment")) for f in entry["flags"])
        if hard or (soft and entry["action"] == KEEP):
            # Low-content deletes are proposed unticked: a human confirms them.
            entry.update(action=DELETE, checked=hard)
        entries.append(entry)

    if use_tfidf:
        apply_tfidf(entries, existing, tfidf_min)
    context = {"vault": vault, "inbox": inbox, "inbox_rel": inbox.relative_to(vault).as_posix(),
               "existing": sorted(existing), "topics": order}
    return entries, context


def apply_tfidf(entries, existing, threshold):
    docs, owners = [], []
    for topic, folder_notes in existing.items():
        for note in folder_notes:
            docs.append(note.search)
            owners.append(("seed", topic))
    for entry in entries:
        docs.append(entry["note"].search)
        owners.append(("entry", entry))
    vectors = tfidf_vectors(docs)
    members = {}
    for (kind, owner), vec in zip(owners, vectors):
        topic = owner if kind == "seed" else (owner["action"] if owner["action"] not in (KEEP, DELETE) else None)
        if topic:
            members.setdefault(topic, []).append(vec)
    centroids = {topic: centroid(vecs) for topic, vecs in members.items()}
    for (kind, owner), vec in zip(owners, vectors):
        if kind != "entry" or owner["action"] != KEEP or not vec:
            continue
        sims = {topic: cosine(vec, c) for topic, c in centroids.items()}
        best = max(sims, key=sims.get, default=None)
        if best and sims[best] >= threshold:
            owner.update(action=best, score=round(sims[best], 2), checked=False)
            owner["why"] = [f"tf-idf similar to {best}/ ({sims[best]:.2f})"]
            owner["flags"].append("low confidence (tf-idf only)")


# ---------- plan file ----------

# The file name runs to the final "]]", so hand-made names containing brackets survive.
ENTRY_RE = re.compile(r"^- \[(?P<check>[ xX])\] (?P<action>`[^`]+`|\S+) ← \[\[(?P<file>.+)\]\]\s*$")


def summarize(note, width=110):
    text = note.text if not note.unavailable else ""
    title = str(note.meta.get("article_title") or "")
    text = (f"{title}: {text}" if title else text) or "(no text)"
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def detail_line(entry):
    note = entry.get("note")
    parts = [f"@{entry['author']}" if entry.get("author") else "@?"]
    if note is not None:
        parts.append(f"“{summarize(note)}”")
        if note.media:
            parts.append(f"{len(note.media)} media")
    if entry["why"]:
        parts.append("why: " + ", ".join(entry["why"]))
    if entry["flags"]:
        parts.append("⚠ " + "; ".join(entry["flags"]))
    return " · ".join(p.replace("\n", " ") for p in parts)


def render_plan(entries, context, generated=None):
    generated = generated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    existing = set(context.get("existing", []))
    groups = {}
    for entry in entries:
        groups.setdefault(entry["action"], []).append(entry)
    topics = sorted((a for a in groups if a not in (KEEP, DELETE)), key=lambda a: (-len(groups[a]), a))
    order = topics + [a for a in (DELETE, KEEP) if a in groups]
    lines = [
        "# X bookmark triage plan", "",
        f"<!-- {PLAN_MARKER} · inbox: {context['inbox_rel']} -->",
        f"Generated {generated} from `{context['inbox_rel']}` ({len(entries)} notes).", "",
        "How to review: change the action word on an entry to any folder name, `delete` (moved to "
        f"`{DELETE_FOLDER}/` in the copy) or `keep` (stays in the inbox); untick `[x]` to skip an entry. "
        "Then run `x-bookmarks triage --vault <vault> --apply --target <copy dir>`, or edit interactively "
        "with `x-bookmarks triage-ui`.", "",
        "## Summary", "", "| Action | Notes | Folder |", "| --- | ---: | --- |",
    ]
    for action in order:
        kind = ("existing" if action in existing else "new") if action not in (KEEP, DELETE) else (
            f"→ `{DELETE_FOLDER}/`" if action == DELETE else "stays in inbox")
        lines.append(f"| {action} | {len(groups[action])} | {kind} |")
    for action in order:
        lines += ["", f"## {action} ({len(groups[action])})", ""]
        for entry in sorted(groups[action], key=lambda e: e["file"]):
            token = f"`{action}`" if re.search(r"\s", action) else action
            lines.append(f"- [{'x' if entry['checked'] else ' '}] {token} ← [[{entry['file'][:-3] if entry['file'].endswith('.md') else entry['file']}]]")
            lines.append(f"  - {entry.get('detail') or detail_line(entry)}")
    return "\n".join(lines) + "\n"


def parse_plan(text):
    match = re.search(rf"<!-- {re.escape(PLAN_MARKER)} · inbox: (.+?) -->", text)
    if not match:
        raise ValueError("Not a triage plan (missing plan marker comment)")
    context = {"inbox_rel": match.group(1).strip(), "existing": []}
    entries, current = [], None
    for line in text.splitlines():
        m = ENTRY_RE.match(line)
        if not m and re.match(r"^- \[[ xX]\] ", line):
            raise ValueError(f"Unreadable plan entry (expected '- [x] <action> ← [[note]]'): {line[:120]}")
        if m:
            action = m.group("action").strip("`").strip()
            if action not in (KEEP, DELETE):
                validate_folder(action)
            current = {"file": m.group("file") + ".md", "action": action, "checked": m.group("check") != " ",
                       "why": [], "flags": [], "detail": ""}
            entries.append(current)
        elif current is not None and line.startswith("  - ") and not current["detail"]:
            current["detail"] = line[4:]
            author = re.match(r"@(\S+)", current["detail"])
            current["author"] = author.group(1) if author and author.group(1) != "?" else ""
        elif line.startswith("| ") and "| existing |" in line:
            context["existing"].append(line.split("|")[1].strip())
        else:
            current = None if line.strip() else current
    files = [e["file"] for e in entries]
    if len(files) != len(set(files)):
        raise ValueError("Plan lists the same note more than once")
    return entries, context


def write_plan(path, entries, context):
    atomic_write(Path(path), render_plan(entries, context).encode())


def guard_outside(path, vault, what):
    path, vault = Path(path).expanduser().resolve(), Path(vault).expanduser().resolve()
    if path == vault or path.is_relative_to(vault) or vault.is_relative_to(path):
        raise ValueError(f"{what} must be outside the vault: {path}")
    return path


# ---------- apply ----------

COPY_MARKER = ".x-bookmarks-triage-copy"


def obsidian_ancestor(path):
    """Return the first directory at or above path that looks like an Obsidian vault."""
    for directory in (path, *path.parents):
        if (directory / ".obsidian").exists():
            return directory
    return None


def apply_plan(vault, plan_path, target):
    """Copy the notes folder (inbox parent) into target and execute the plan there.

    Only a copy this command created (marked with COPY_MARKER) is ever modified, and
    every path touched must resolve inside that copy and outside the source vault, so
    a wrong --vault, a real vault given as --target, or symlinks cannot move real notes.
    """
    vault = Path(vault).expanduser().resolve()
    target = guard_outside(target, vault, "--target")
    vault_like = obsidian_ancestor(target)
    if vault_like is not None:
        raise ValueError(f"--target is inside an Obsidian vault ({vault_like}); use an empty directory elsewhere")
    entries, context = parse_plan(Path(plan_path).read_text(encoding="utf-8"))
    source_inbox = (vault / context["inbox_rel"]).resolve()
    if not source_inbox.is_relative_to(vault) or source_inbox == vault:
        raise ValueError("Plan inbox path escapes the vault")
    notes_rel = source_inbox.parent.relative_to(vault)
    copy_root = (target / notes_rel).resolve()
    if not copy_root.is_relative_to(target):
        raise ValueError("Plan inbox path escapes the target")
    copied = False
    if not copy_root.exists():
        if not source_inbox.is_dir():
            raise ValueError(f"Source inbox not found: {source_inbox}")
        shutil.copytree(source_inbox.parent, copy_root, symlinks=True)
        atomic_write(copy_root / COPY_MARKER, json.dumps(
            {"source_vault": str(vault), "notes": notes_rel.as_posix(),
             "copied_at": datetime.now(timezone.utc).isoformat()}).encode())
        copied = True
    elif not (copy_root / COPY_MARKER).is_file():
        raise ValueError(f"{copy_root} exists but was not created by triage --apply "
                         f"(no {COPY_MARKER}); refusing to modify it. Use a new, empty --target.")
    if obsidian_ancestor(copy_root) is not None or any(copy_root.rglob(".obsidian")):
        raise ValueError(f"{copy_root} contains Obsidian configuration; refusing to modify it")

    def safe(path):
        resolved = path.resolve()
        return resolved.is_relative_to(copy_root) and not resolved.is_relative_to(vault)

    inbox = copy_root / source_inbox.name
    if inbox.is_symlink() or not inbox.is_dir() or not safe(inbox) or inbox.resolve().parent != copy_root:
        raise ValueError(f"Target inbox is missing, a symlink, or outside the copy: {inbox}")
    inbox = inbox.resolve()
    log = {"applied_at": datetime.now(timezone.utc).isoformat(), "plan": str(Path(plan_path).resolve()),
           "copied_from_vault": copied, "moved": [], "skipped": []}
    for entry in entries:
        if not entry["checked"] or entry["action"] == KEEP:
            continue
        folder = DELETE_FOLDER if entry["action"] == DELETE else validate_folder(entry["action"])
        source = inbox / entry["file"]
        folder_path = copy_root / folder
        destination = folder_path / entry["file"]
        if (source.parent != inbox or source.is_symlink() or not safe(source)
                or folder_path.is_symlink() or not safe(folder_path) or not safe(destination)
                or destination.resolve().parent != folder_path.resolve()):
            log["skipped"].append({"file": entry["file"], "reason": "unsafe path"})
            continue
        if not source.is_file():
            log["skipped"].append({"file": entry["file"], "reason": "not in inbox (already moved?)"})
            continue
        if destination.exists():
            log["skipped"].append({"file": entry["file"], "reason": f"already exists in {folder}/"})
            continue
        folder_path.mkdir(exist_ok=True)
        os.rename(source, destination)
        log["moved"].append({"file": entry["file"], "to": folder})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    content = json.dumps(log, ensure_ascii=False, indent=2).encode()
    for counter in range(1000):
        log_path = copy_root / f"triage-applied-{stamp}{f'-{counter}' if counter else ''}.json"
        try:
            with open(log_path, "xb") as stream:  # never overwrite an earlier run's log
                stream.write(content)
            break
        except FileExistsError:
            continue
    return log, copy_root, log_path


def run_triage(args):
    vault = Path(args.vault).expanduser().resolve()
    plan_path = guard_outside(args.plan, vault, "--plan")
    if args.apply:
        if not args.target:
            raise ValueError("--apply requires --target <copy dir> (the source vault is never modified)")
        log, copy_root, log_path = apply_plan(vault, plan_path, args.target)
        counts = Counter(m["to"] for m in log["moved"])
        if log["copied_from_vault"]:
            print(f"Copied notes folder to {copy_root}")
        for folder, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {folder:<16} {count:>4} moved")
        print(f"Moved {len(log['moved'])}, skipped {len(log['skipped'])}. Log: {log_path}")
        return
    entries, context = build_plan(vault, args.inbox, load_rules(args.rules), args.tfidf, args.tfidf_min)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    write_plan(plan_path, entries, context)
    counts = Counter(e["action"] for e in entries)
    print(f"Triage plan for {len(entries)} notes in {context['inbox_rel']}:")
    for action, count in sorted(counts.items(), key=lambda kv: (kv[0] in (KEEP, DELETE), -kv[1], kv[0])):
        label = "existing" if action in context["existing"] else ("" if action in (KEEP, DELETE) else "new")
        print(f"  {action:<16} {count:>4}  {label}")
    print(f"Wrote {plan_path}")
