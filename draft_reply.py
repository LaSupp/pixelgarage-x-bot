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
MAX_ISSUES_PER_RUN = 2

# Update these search queries or target handles to match your @pixelgaragedata niche
SEARCH_QUERIES = [
    '("data science" OR "statistics" OR "p-value" OR "machine learning") -is:retweet -is:reply lang:en',
    '("NFL analytics" OR "EPA per play" OR "expected points") -is:retweet -is:reply lang:en',
]

# Working Gemini models in priority order (adjust if your Bluesky script uses specific IDs)
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]

SYSTEM_PROMPT = """You are @pixelgaragedata (Pixel Garage Graphics), a sharp, witty data scientist and sports/data nerd.
Write 3 distinct, engaging reply options to the X post below.

Guidelines:
- Keep every reply strictly under 240 characters.
- Be insightful, dryly funny, or add a crisp statistical/analytical angle—never generic praise ("Great post!") or spammy self-promotion.
- Vary the angles across the 3 options (e.g., 1: witty statistical observation, 2: sharp analytical question, 3: relatable data-nerd joke).
- Do not use hashtags unless strictly necessary.

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
                        temperature=0.8,
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

    # Verify self user ID so we never reply to our own posts
    me = x_client.get_me(user_auth=True)
    my_user_id = str(me.data.id) if me and me.data else ""

    candidates = []
    query = random.choice(SEARCH_QUERIES)
    print(f"Searching X with query: {query}")

    resp = x_client.search_recent_tweets(
        query=query,
        max_results=15,
        tweet_fields=["author_id", "public_metrics", "created_at"],
        expansions=["author_id"],
        user_fields=["username"],
        user_auth=True,
    )

    if not resp or not resp.data:
        print("No tweets returned from search.")
        return

    users_by_id = {str(u.id): u.username for u in (resp.includes.get("users", []) if resp.includes else [])}

    for tweet in resp.data:
        tid = str(tweet.id)
        author_id = str(tweet.author_id)
        if tid in seen_set or author_id == my_user_id:
            continue
        username = users_by_id.get(author_id, "i")
        candidates.append((tid, username, tweet.text))

    if not candidates:
        print("No new unseen candidate tweets found.")
        return

    drafted_count = 0
    for tid, username, text in candidates:
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
