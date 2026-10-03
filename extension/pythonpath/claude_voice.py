"""My voice: learn how the user writes from their own samples, then rewrite in that voice.

Nothing here talks to Claude by itself or touches UNO; claude_panel.py supplies both.
The samples and the profile live in claude-voice.json beside the settings file, on this
computer only. They are sent to Claude only with requests that use the voice, and when
the user asks Claude to (re)learn it.
"""

import hashlib
import json
import os
import time

FILE_NAME = "claude-voice.json"
MAX_SAMPLE_CHARS = 20000     # per sample, when learning
MAX_LEARN_CHARS = 60000      # all samples together, when learning
MAX_EXCERPT_CHARS = 6000     # of the user's own writing sent with each voice rewrite
MIN_SAMPLE_WORDS = 20

LEARN_SYSTEM = (
    "You study writing samples that one person wrote and describe how they write, so that someone "
    "else could rewrite text to sound exactly like them. Reply with 6 to 10 short bullet points, "
    "each starting with \"- \", and nothing else. Cover what the samples actually show: sentence "
    "length and structure, register and vocabulary, regional usage (e.g. Canadian French), how they "
    "address the reader, punctuation and formatting habits, how they open and close paragraphs, and "
    "recurring words or turns of phrase. Be concrete and quote short examples from the samples. "
    "Describe; do not praise or advise. Write the bullets in the language most samples are in.")

VOICE_REWRITE = (
    "Rewrite the selected text the way the user would write it themselves, following their writing "
    "profile and the excerpts of their own writing given in the system prompt. Match their "
    "vocabulary, sentence length, punctuation and register, and don't make it more polished, formal "
    "or elaborate than they would. Keep the meaning, and keep the same language unless asked "
    "otherwise. Reply with only the rewritten text.")

FORMAL_REWRITE = (
    "Rewrite the selected text in a more formal, professional tone. Keep the meaning, and keep the "
    "same language unless asked otherwise. Reply with only the rewritten text.")


def path_for(settings_path):
    return os.path.join(os.path.dirname(settings_path), FILE_NAME)


def empty():
    return {"samples": [], "profile": "", "updated": 0, "learned_ids": [], "default": False}


def load(path):
    data = empty()
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            data.update({k: stored[k] for k in data if k in stored})
    except (OSError, ValueError):
        pass
    return data


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_sample(data, name, source, text):
    """Add a writing sample. Returns (sample, None) or (None, reason)."""
    text = (text or "").strip()
    words = len(text.split())
    if words < MIN_SAMPLE_WORDS:
        return None, "That's only %d words. Add at least %d words of your own writing." % (words, MIN_SAMPLE_WORDS)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if any(s["id"] == digest for s in data["samples"]):
        return None, "That text is already one of your samples."
    sample = {"id": digest, "name": name, "source": source, "text": text, "words": words, "added": time.time()}
    data["samples"].append(sample)
    return sample, None


def remove_sample(data, sample_id):
    data["samples"] = [s for s in data["samples"] if s["id"] != sample_id]


def is_stale(data):
    """True when the samples changed since the profile was learned."""
    return sorted(s["id"] for s in data["samples"]) != sorted(data.get("learned_ids") or [])


def summary(data):
    """What the panel shows: everything except the samples' full text."""
    samples = []
    for s in data["samples"]:
        used = min(len(s["text"]), MAX_SAMPLE_CHARS)
        samples.append({"id": s["id"], "name": s["name"], "source": s["source"], "words": s["words"],
                        "partly_used": used < len(s["text"])})
    return {"samples": samples, "profile": data["profile"], "updated": data["updated"],
            "default": bool(data["default"]), "stale": bool(data["samples"]) and is_stale(data),
            "ready": bool(data["profile"].strip())}


def learn_request(data):
    """(system, user text) asking Claude to describe the user's writing."""
    parts, budget = [], MAX_LEARN_CHARS
    for i, s in enumerate(data["samples"], 1):
        text = s["text"][:min(MAX_SAMPLE_CHARS, budget)]
        if not text:
            break
        budget -= len(text)
        parts.append("<sample %d>\n%s\n</sample %d>" % (i, text, i))
    user = ("Here are writing samples by the same person:\n\n%s\n\nDescribe how this person writes."
            % "\n\n".join(parts))
    return LEARN_SYSTEM, user


def finish_learning(data, profile):
    data["profile"] = profile.strip()
    data["updated"] = time.time()
    data["learned_ids"] = [s["id"] for s in data["samples"]]


def system_addition(data):
    """Appended to the system prompt of a request written in the user's voice."""
    excerpts, budget = [], MAX_EXCERPT_CHARS
    for s in data["samples"]:
        if budget <= 0:
            break
        text = s["text"][:budget]
        budget -= len(text)
        excerpts.append("<excerpt>\n%s\n</excerpt>" % text)
    return ("\n\nWrite as the user writes. Their writing profile:\n<profile>\n%s\n</profile>\n\n"
            "Excerpts of their own writing:\n%s" % (data["profile"].strip(), "\n".join(excerpts)))
