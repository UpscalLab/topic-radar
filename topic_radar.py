#!/usr/bin/env python3
"""
Topic Radar v2 — Veille gaming/tech/IA via Reddit
Scanne les subreddits, détecte les tendances, alerte sur Discord + Ntfy.
"""
 
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
 
import requests
from dotenv import load_dotenv
 
# ── Configuration ──────────────────────────────────────────────
 
load_dotenv()
 
# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
 
# Discord
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
 
# Ntfy
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
 
# Reddit
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "UpscaleLabTopicRadar/2.0")
 
# Paramètres
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "14400"))  # 4h
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "7"))
STATE_FILE = os.getenv("STATE_FILE", "topic_radar_state.json")
REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
 
# Subreddits à surveiller
SUBREDDITS = [
    # Gaming
    "gaming", "pcgaming", "Games", "PS5", "XboxSeriesX",
    "NintendoSwitch", "Steam", "gamedev", "IndieGaming",
    # Tech
    "technology", "gadgets", "hardware", "buildapc", "selfhosted",
    # IA
    "artificial", "MachineLearning", "ChatGPT", "ClaudeAI",
    "StableDiffusion", "LocalLLaMA", "singularity",
]
 
# ── Logging ────────────────────────────────────────────────────
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("TopicRadar")
 
# ── État persistant ────────────────────────────────────────────
 
 
def load_state() -> dict:
    """Charge l'état précédent (posts déjà vus)."""
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_ids": [], "last_scan": None, "scan_count": 0}
 
 
def save_state(state: dict):
    """Sauvegarde l'état courant."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
 
 
# ── Reddit (RSS + JSON fallback) ───────────────────────────────
 
 
def fetch_subreddit_rss(subreddit: str) -> list[dict]:
    """Récupère les posts via RSS (fonctionne depuis les datacenters)."""
    import xml.etree.ElementTree as ET
 
    url = f"https://www.reddit.com/r/{subreddit}/hot/.rss"
    headers = {"User-Agent": REDDIT_USER_AGENT}
 
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
 
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
 
        posts = []
        for entry in root.findall("atom:entry", ns):
            title = entry.find("atom:title", ns)
            link = entry.find("atom:link", ns)
            entry_id = entry.find("atom:id", ns)
            updated = entry.find("atom:updated", ns)
 
            post_id = ""
            if entry_id is not None and entry_id.text:
                # Extrait l'ID du post depuis l'URL
                parts = entry_id.text.rstrip("/").split("/")
                post_id = parts[-1] if parts else ""
 
            post_url = link.get("href", "") if link is not None else ""
 
            posts.append({
                "id": post_id,
                "subreddit": subreddit,
                "title": title.text if title is not None else "",
                "score": 0,  # RSS ne fournit pas le score
                "num_comments": 0,  # RSS ne fournit pas les commentaires
                "url": post_url,
                "created_utc": 0,
            })
        return posts
 
    except Exception as e:
        log.warning(f"Erreur RSS r/{subreddit}: {e}")
        return []
 
 
def fetch_subreddit_json(subreddit: str, limit: int = 25) -> list[dict]:
    """Récupère les posts via l'endpoint .json (fallback si RSS échoue)."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    headers = {"User-Agent": REDDIT_USER_AGENT}
 
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
 
        posts = []
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            posts.append({
                "id": p.get("id", ""),
                "subreddit": subreddit,
                "title": p.get("title", ""),
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "created_utc": p.get("created_utc", 0),
            })
        return posts
 
    except Exception as e:
        log.warning(f"Erreur JSON r/{subreddit}: {e}")
        return []
 
 
def fetch_subreddit(subreddit: str, limit: int = 25) -> list[dict]:
    """Récupère les posts d'un subreddit (RSS d'abord, JSON en fallback)."""
    # Essaie RSS d'abord (fonctionne depuis les datacenters)
    posts = fetch_subreddit_rss(subreddit)
    if posts:
        return posts
 
    # Fallback JSON (fonctionne en local)
    return fetch_subreddit_json(subreddit, limit)
 
 
def fetch_all_subreddits() -> list[dict]:
    """Scanne tous les subreddits configurés avec un délai entre chaque."""
    all_posts = []
    for sub in SUBREDDITS:
        log.info(f"Scan r/{sub}...")
        posts = fetch_subreddit(sub)
        all_posts.extend(posts)
        time.sleep(2)  # Respecte le rate limit
    log.info(f"Total: {len(all_posts)} posts récupérés")
    return all_posts
 
 
# ── Filtrage des nouveaux posts ────────────────────────────────
 
 
def filter_new_posts(posts: list[dict], state: dict) -> list[dict]:
    """Filtre les posts déjà vus et garde les plus engageants."""
    seen = set(state.get("seen_ids", []))
    new_posts = [p for p in posts if p["id"] not in seen]
 
    # Trie par engagement (score + commentaires)
    new_posts.sort(key=lambda p: p["score"] + p["num_comments"], reverse=True)
 
    # Met à jour les IDs vus (garde les 5000 derniers pour limiter la mémoire)
    all_ids = list(seen | {p["id"] for p in new_posts})
    state["seen_ids"] = all_ids[-5000:]
 
    log.info(f"Nouveaux posts: {len(new_posts)} (déjà vus: {len(seen)})")
    return new_posts
 
 
# ── Analyse Anthropic ──────────────────────────────────────────
 
 
def analyze_trends(posts: list[dict]) -> dict:
    """Utilise Claude pour analyser les tendances à partir des posts."""
    if not ANTHROPIC_API_KEY:
        log.warning("Pas de clé Anthropic, analyse ignorée")
        return {"summary": "Analyse indisponible (clé API manquante)", "topics": [], "alerts": []}
 
    # Prépare le contexte pour Claude (top 50 posts)
    top_posts = posts[:50]
    posts_text = "\n".join(
        f"- [r/{p['subreddit']}] {p['title']}"
        + (f" (score: {p['score']}, commentaires: {p['num_comments']})" if p.get('score', 0) > 0 else "")
        for p in top_posts
    )
 
    prompt = f"""Analyse ces posts Reddit trending en gaming, tech et IA.
Identifie les tendances émergentes et sujets importants pour une chaîne YouTube gaming/tech.
 
Posts:
{posts_text}
 
Réponds en JSON strict (pas de markdown) avec cette structure:
{{
  "summary": "Résumé en 2-3 phrases des tendances principales",
  "topics": [
    {{
      "name": "Nom du sujet/tendance",
      "relevance": 1-10,
      "category": "gaming|tech|ia|youtube",
      "description": "Description courte",
      "video_potential": "Idée de vidéo YouTube potentielle"
    }}
  ],
  "alerts": [
    {{
      "title": "Sujet urgent/viral",
      "reason": "Pourquoi c'est important maintenant",
      "urgency": 1-10
    }}
  ]
}}
 
Limite à 5-8 topics et 0-3 alertes. Ne retourne que du JSON valide."""
 
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
 
        # Parse le JSON de la réponse
        # Nettoie si Claude a mis des backticks
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        content = content.strip()
 
        analysis = json.loads(content)
        log.info(f"Analyse: {len(analysis.get('topics', []))} topics, {len(analysis.get('alerts', []))} alertes")
        return analysis
 
    except json.JSONDecodeError as e:
        log.error(f"Erreur parsing JSON Claude: {e}")
        return {"summary": "Erreur d'analyse", "topics": [], "alerts": []}
    except Exception as e:
        log.error(f"Erreur Anthropic API: {e}")
        return {"summary": "Erreur d'analyse", "topics": [], "alerts": []}
 
 
# ── Discord ────────────────────────────────────────────────────
 
 
def send_discord(analysis: dict, post_count: int):
    """Envoie le rapport de tendances sur Discord."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("Pas de webhook Discord configuré")
        return
 
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
 
    # Embed principal
    topics_text = ""
    for t in analysis.get("topics", [])[:8]:
        emoji = {"gaming": "🎮", "tech": "💻", "ia": "🤖", "youtube": "🎬"}.get(t.get("category", ""), "📌")
        relevance = t.get("relevance", 0)
        bar = "█" * relevance + "░" * (10 - relevance)
        topics_text += f"{emoji} **{t['name']}** [{bar}] {relevance}/10\n"
        topics_text += f"  └ {t.get('description', '')}\n"
        if t.get("video_potential"):
            topics_text += f"  🎥 *{t['video_potential']}*\n"
        topics_text += "\n"
 
    embed = {
        "title": "📡 Topic Radar — Scan",
        "description": analysis.get("summary", ""),
        "color": 0x6C5CE7,
        "fields": [
            {
                "name": "🔥 Tendances détectées",
                "value": topics_text[:1024] if topics_text else "Aucune tendance notable",
                "inline": False,
            },
        ],
        "footer": {"text": f"Scan: {now} • {post_count} posts analysés"},
    }
 
    # Alertes
    alerts = analysis.get("alerts", [])
    if alerts:
        alerts_text = ""
        for a in alerts:
            urgency = a.get("urgency", 0)
            icon = "🚨" if urgency >= 8 else "⚠️" if urgency >= 5 else "ℹ️"
            alerts_text += f"{icon} **{a['title']}** (urgence: {urgency}/10)\n"
            alerts_text += f"  └ {a.get('reason', '')}\n\n"
        embed["fields"].append({
            "name": "🚨 Alertes",
            "value": alerts_text[:1024],
            "inline": False,
        })
 
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Discord: rapport envoyé ✓")
    except Exception as e:
        log.error(f"Erreur Discord: {e}")
 
 
# ── Ntfy ───────────────────────────────────────────────────────
 
 
def send_ntfy(analysis: dict):
    """Envoie une notification push via Ntfy pour les alertes urgentes."""
    if not NTFY_TOPIC:
        log.warning("Pas de topic Ntfy configuré")
        return
 
    alerts = [a for a in analysis.get("alerts", []) if a.get("urgency", 0) >= ALERT_THRESHOLD]
 
    if not alerts:
        log.info("Ntfy: pas d'alerte urgente, pas de notification")
        return
 
    # Envoie une notif par alerte urgente
    for alert in alerts:
        title = f"Topic Radar: {alert['title']}"
        message = alert.get("reason", "Nouvelle tendance détectée")
 
        try:
            resp = requests.post(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                headers={"Title": title, "Priority": "high", "Tags": "rotating_light,chart_with_upwards_trend"},
                data=message.encode("utf-8"),
                timeout=10,
            )
            resp.raise_for_status()
            log.info(f"Ntfy: alerte envoyée ✓ — {alert['title']}")
        except Exception as e:
            log.error(f"Erreur Ntfy: {e}")
 
 
# ── Rapport local ──────────────────────────────────────────────
 
 
def save_report(analysis: dict, posts: list[dict]):
    """Sauvegarde un rapport JSON local."""
    Path(REPORTS_DIR).mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(REPORTS_DIR) / f"scan_{timestamp}.json"
 
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "post_count": len(posts),
        "analysis": analysis,
        "top_posts": posts[:20],
    }
 
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
 
    log.info(f"Rapport sauvé: {report_path}")
 
 
# ── Boucle principale ─────────────────────────────────────────
 
 
def run_scan():
    """Exécute un scan complet."""
    log.info("=" * 50)
    log.info("🚀 Démarrage du scan Topic Radar")
    log.info("=" * 50)
 
    # 1. Charger l'état
    state = load_state()
 
    # 2. Récupérer les posts Reddit
    all_posts = fetch_all_subreddits()
    if not all_posts:
        log.error("Aucun post récupéré, abandon du scan")
        return
 
    # 3. Filtrer les nouveaux posts
    new_posts = filter_new_posts(all_posts, state)
 
    # 4. Analyser avec Claude
    analysis = analyze_trends(new_posts if new_posts else all_posts)
 
    # 5. Envoyer sur Discord
    send_discord(analysis, len(all_posts))
 
    # 6. Envoyer les alertes Ntfy
    send_ntfy(analysis)
 
    # 7. Sauvegarder le rapport
    save_report(analysis, new_posts[:20])
 
    # 8. Mettre à jour l'état
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["scan_count"] = state.get("scan_count", 0) + 1
    save_state(state)
 
    log.info("✅ Scan terminé")
 
 
def main():
    """Point d'entrée principal."""
    # Vérifications
    if not ANTHROPIC_API_KEY:
        log.warning("⚠️  ANTHROPIC_API_KEY manquante — l'analyse IA sera désactivée")
    if not DISCORD_WEBHOOK_URL:
        log.warning("⚠️  DISCORD_WEBHOOK_URL manquante — pas de notifications Discord")
    if not NTFY_TOPIC:
        log.warning("⚠️  NTFY_TOPIC manquant — pas de notifications push")
 
    if "--once" in sys.argv:
        # Mode single scan
        run_scan()
    else:
        # Mode boucle continue
        log.info(f"Topic Radar v2 — Intervalle: {SCAN_INTERVAL}s ({SCAN_INTERVAL // 3600}h)")
        log.info(f"Subreddits surveillés: {', '.join(SUBREDDITS)}")
        while True:
            try:
                run_scan()
                log.info(f"Prochain scan dans {SCAN_INTERVAL // 60} minutes...")
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                log.info("Arrêt demandé par l'utilisateur")
                break
            except Exception as e:
                log.error(f"Erreur inattendue: {e}")
                log.info("Nouvelle tentative dans 60s...")
                time.sleep(60)
 
 
if __name__ == "__main__":
    main()
