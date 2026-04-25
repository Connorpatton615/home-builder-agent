import os
from dotenv import load_dotenv
from anthropic import Anthropic

# Load the API key from .env file
load_dotenv()

# Create the client (the object that talks to Claude's API)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Send a message to Claude and get the response back
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    messages=[
        {"role": "user", "content": "In one sentence, what's the most underrated step in residential home construction?"}
    ],
)

# Print Claude's reply
print("Claude says:")
print(response.content[0].text)
