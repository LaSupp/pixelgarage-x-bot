import os
import json
import time
import random
import requests
import tweepy
from google import genai
from google.genai import types

# --- Configuration ---
SEEN_FILE = "seen_ids.json"
MAX_SEEN_IDS = 500
MAX_ISSUES_PER_RUN = 3
MIN_AUTHOR_FOLLOWERS = 500
MIN_ENGAGEMENT_SCORE = 5

# Multi-signal queries that avoid gaming, crypto, and generic buzzword spam
SEARCH_QUERIES = [
    # 1. Data visualization & storytelling
    '("data visualization" OR "dataviz" OR "ggplot" OR "matplotlib" OR "scatter plot") ("chart" OR "data" OR "plot" OR "distribution") -is:retweet -is:reply -crypto -airdrop -giveaway lang:en',
    # 2. Applied statistics, experimentation & causal inference
    '("causal inference" OR "p-value" OR "bayesian" OR "confidence interval" OR "sample size" OR "A/B test") ("data" OR "model" OR "experiment" OR "regression" OR "metric") -is:retweet -is:reply -roblox -gaming -crypto lang:en',
    # 3. Practical ML & data science workflows
    '("feature engineering" OR "overfitting" OR "cross-validation" OR "XGBoost" OR "logistic regression" OR "time series") ("model" OR "data" OR "dataset" OR "accuracy") -is:retweet -is:reply -crypto -hiring lang:en',
    # 4. NFL analytics & football modeling
    '("EPA per play" OR "CPOE" OR "DVOA" OR "win probability" OR "expected points" OR "success rate") ("NFL" OR "QB" OR "offense" OR "defense") -is:retweet -is:reply lang:en',
]

FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]

SYSTEM_PROMPT = """You are @pixelgaragedata (Pixel Garage Graphics), a Principal Data Scientist and sports analytics nerd who builds clean charts and statistical models.
Write 3 distinct, natural reply options to the X post below.

Voice & Tone Rules:
- Sound like a sharp, helpful peer in the data/analytics community—conversational, curious, and grounded.
- Do NOT be snarky, condescending, or cynical, and do NOT force jokes on serious or clinical topics.
- Keep every reply strictly under 240 characters.
- Never use generic filler ("Great point!", "Love this!", "So true!") or self-promotion.
- Do not use hashtags.
- Do not use hyphens or em dashes
- avoid sounding like AI

Vary the 3 options across these distinct angles:
1. Analytical Nuance: Point out a practical modeling detail, edge case, or real-world caveat that adds genuine value to the post.
2. Sharp Follow-Up Question: Ask a specific, thoughtful question about their methodology, data source, or metric choice that invites a reply.
3. Light/Relatable Take: Share a concise, relatable practitioner observation (keep it warm and clever, never dismissive).

Return ONLY valid JSON in this exact format:
{
  "options": [
    "First reply option here",
    "Second reply option here",
    "Third reply option here"
  ]
}
"""


def load_seen_ids() -> list[str]:
    if not os.path.exists(SEEN_FILE):
        return []
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def save_seen_ids(seen_ids: list[str]) -> None:
    trimmed = seen_ids[-MAX_SEEN_IDS:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2)


def get_x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=True,
    )


def engagement_score(tweet) -> int:
    metrics = getattr(tweet, "public_metrics", None) or {}
    likes = metrics.get("like_count", 0)
    replies = metrics.get("reply_count", 0)
    retweets = metrics.get("retweet_count", 0)
    quotes = metrics.get("quote_count", 0)
    return likes + (replies * 3) + (retweets * 2) + (quotes * 2)


def generate_replies(gemini_client: genai.Client, author_username: str, tweet_text: str) -> list[str]:
    prompt = f"Post by @{author_username}:\n\"\"\"\n{tweet_text}\n\"\"\""

    for model_name in FALLBACK_MODELS:
        for attempt in range(3):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.7,
                    ),
                )
                parsed = json.loads(response.text)
                options = parsed.get("options", [])
                clean_options = [opt.strip()[:275] for opt in options if isinstance(opt, str) and opt.strip()]
                if len(clean_options) >= 3:
                    return clean_options[:3]
            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "429" in err_str or "overloaded" in err_str.lower():
                    print(f"[{model_name}] Transient error ({err_str}), waiting 15s before retry...")
                    time.sleep(15)
                    continue
                print(f"[{model_name}] Failed: {err_str}")
                break

    raise RuntimeError("All Gemini fallback models failed to generate replies.")


def create_github_issue(tweet_id: str, author_username: str, tweet_text: str, options: list[str]) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"

    body = (
        f"### Target Post\n"
        f"**Author:** @{author_username}\n"
        f"**Link:** {tweet_url}\n"
        f"**Tweet ID:** `{tweet_id}`\n\n"
        f"> {tweet_text}\n\n"
        f"---\n\n"
        f"### Reply Options\n"
        f"**1:** {options[0]}\n\n"
        f"**2:** {options[1]}\n\n"
        f"**3:** {options[2]}\n\n"
        f"---\n"
        f"Comment `1`, `2`, `3`, or type your own custom reply text to publish. Comment `skip` to close without posting."
    )

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"X Reply Draft: @{author_username} ({tweet_id})",
            "body": body,
            "labels": ["reply-draft"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Created GitHub Issue #{resp.json()['number']} for tweet {tweet_id}")


def main() -> None:
    seen_ids = load_seen_ids()
    seen_set = set(seen_ids)

    x_client = get_x_client()
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    me = x_client.get_me(user_auth=True)
    my_user_id = str(me.data.id) if me and me.data else ""

    query = random.choice(SEARCH_QUERIES)
    print(f"Searching X with query: {query}")

    resp = x_client.search_recent_tweets(
        query=query,
        max_results=100,
        tweet_fields=["author_id", "public_metrics", "created_at", "reply_settings"],
        expansions=["author_id"],
        user_fields=["username", "public_metrics"],
        user_auth=True,
    )

    if not resp or not resp.data:
        print("No tweets returned from search.")
        return

    users_by_id = {}
    for u in (resp.includes.get("users", []) if resp.includes else []):
        u_metrics = getattr(u, "public_metrics", None) or {}
        users_by_id[str(u.id)] = {
            "username": u.username,
            "followers": u_metrics.get("followers_count", 0),
        }

    candidates = []
    for tweet in resp.data:
        tid = str(tweet.id)
        author_id = str(tweet.author_id)
        if getattr(tweet, "reply_settings", "everyone") != "everyone":
            continue
        if tid in seen_set or author_id == my_user_id:
            continue
        if len(tweet.text.strip()) < 60:
            continue

        user_info = users_by_id.get(author_id, {"username": "i", "followers": 0})
        if user_info["followers"] < MIN_AUTHOR_FOLLOWERS:
            continue

        score = engagement_score(tweet)
        if score < MIN_ENGAGEMENT_SCORE:
            continue

        candidates.append((score, tid, user_info["username"], tweet.text))

    if not candidates:
        print("No new unseen candidate tweets found.")
        return

    # Prioritize posts that already have likes/replies instead of random zero-engagement posts
    candidates.sort(key=lambda item: item[0], reverse=True)

    drafted_count = 0
    for score, tid, username, text in candidates:
        if drafted_count >= MAX_ISSUES_PER_RUN:
            break
        try:
            options = generate_replies(gemini_client, username, text)
            create_github_issue(tid, username, text, options)
            seen_ids.append(tid)
            seen_set.add(tid)
            drafted_count += 1
        except Exception as e:
            print(f"Error processing tweet {tid}: {e}")

    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()
