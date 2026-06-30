"""
Token Optimisation Proxy — Python Developer Starter Kit (Basic)

Replace PROXY_ENDPOINT and PROXY_API_KEY with values from your platform team.
Do NOT use LLM provider keys (OpenAI, Anthropic, etc.) — the proxy handles all of that.

Usage:
    export PROXY_API_KEY=<your-proxy-key>
    export PROXY_ENDPOINT=https://token-proxy-<hash>-uc.a.run.app
    python agent_basic.py
"""
import os
from openai import OpenAI

PROXY_ENDPOINT = os.environ["PROXY_ENDPOINT"]   # provided by platform team
PROXY_API_KEY  = os.environ["PROXY_API_KEY"]    # provided by platform team — NOT an OpenAI key

client = OpenAI(
    api_key=PROXY_API_KEY,
    base_url=f"{PROXY_ENDPOINT}/v1",
)


def ask(prompt: str, model: str = "gpt-4o-mini") -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        extra_body={
            "user": os.environ.get("USER", "dev"),  # for per-user savings tracking
        },
    )
    return response.choices[0].message.content


def ask_with_session(prompt: str, session_id: str, model: str = "gpt-4o-mini") -> str:
    """Multi-turn conversation — G10 memory management applied automatically."""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        extra_body={
            "user": os.environ.get("USER", "dev"),
            "x_session_id": session_id,  # enables G10 sliding window memory
        },
    )
    savings = response.model_extra.get("_token_opt", {})
    print(f"  Saved: {savings.get('total_abs_saving', 0)} tokens "
          f"({savings.get('total_pct_saving', 0):.1f}%)")
    return response.choices[0].message.content


if __name__ == "__main__":
    print(ask("What is the capital of France?"))
    print(ask_with_session("Summarise the history of Python.", session_id="demo-session-1"))
