import os
import re
import requests
import tweepy


def get_x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def get_issue(repo: str, issue_number: str, token: str) -> dict:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def comment_and_close(repo: str, issue_number: str, token: str, message: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    requests.post(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
        headers=headers,
        json={"body": message},
        timeout=30,
    ).raise_for_status()

    requests.patch(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers=headers,
        json={"state": "closed"},
        timeout=30,
    ).raise_for_status()


def post_comment_only(repo: str, issue_number: str, token: str, message: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    requests.post(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
        headers=headers,
        json={"body": message},
        timeout=30,
    ).raise_for_status()


def parse_issue_body(body: str) -> tuple[str, dict[str, str]]:
    tweet_id_match = re.search(r"\*\*Tweet ID:\*\*\s*`(\d+)`", body)
    if not tweet_id_match:
        raise ValueError("Could not find Tweet ID in issue body.")
    tweet_id = tweet_id_match.group(1)

    options: dict[str, str] = {}
    for num in ("1", "2", "3"):
        opt_match = re.search(rf"\*\*{num}:\*\*\s*(.+)", body)
        if opt_match:
            options[num] = opt_match.group(1).strip()

    return tweet_id, options


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    issue_number = os.environ["ISSUE_NUMBER"]
    comment_body = os.environ.get("COMMENT_BODY", "").strip()

    if not comment_body:
        print("Empty comment body; exiting.")
        return

    if comment_body.lower() in {"skip", "ignore", "close"}:
        comment_and_close(repo, issue_number, token, "Skipped. Closing issue without posting.")
        return

    issue = get_issue(repo, issue_number, token)
    tweet_id, options = parse_issue_body(issue.get("body", ""))

    if comment_body in options:
        reply_text = options[comment_body]
    else:
        reply_text = comment_body

    if len(reply_text) > 280:
        post_comment_only(
            repo,
            issue_number,
            token,
            f"Reply exceeds 280 characters ({len(reply_text)} chars). Please comment a shorter version or pick `1`, `2`, or `3`.",
        )
        return

    x_client = get_x_client()
    try:
        response = x_client.create_tweet(
            text=reply_text,
            in_reply_to_tweet_id=tweet_id,
            user_auth=True,
        )
    except tweepy.Forbidden as e:
        detail = getattr(e, "detail", None) or str(e)
        comment_and_close(
            repo,
            issue_number,
            token,
            f"Could not publish reply — X returned `403 Forbidden` (the author likely restricted replies or deleted the post).\n\n> {detail}\n\nClosing issue.",
        )
        return
    except tweepy.TweepyException as e:
        post_comment_only(
            repo,
            issue_number,
            token,
            f"X API error while publishing reply (issue kept open so you can retry):\n\n> {e}",
        )
        raise

    posted_id = response.data["id"]
    posted_url = f"https://x.com/pixelgaragedata/status/{posted_id}"

    comment_and_close(
        repo,
        issue_number,
        token,
        f"Published reply to X:\n{posted_url}\n\n> {reply_text}",
    )


if __name__ == "__main__":
    main()
