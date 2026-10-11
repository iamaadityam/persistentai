
from dotenv import load_dotenv
from sarvamai import SarvamAI
import os

load_dotenv()

key = os.getenv("SARVAM_API_KEY")
assert key, "SARVAM_API_KEY missing from .env"

client = SarvamAI(api_subscription_key=key)

response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[
        {
            "role": "user",
            "content": "Reply with exactly: Sarvam is working."
        }
    ],
    reasoning_effort=None,
    max_tokens=200,
    temperature=0.2,
)

print("FULL RESPONSE:")
print(response)

print("\nMESSAGE CONTENT:")
print(repr(response.choices[0].message.content))

print("\nFINISH REASON:")
print(response.choices[0].finish_reason)
