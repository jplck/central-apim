"""Invoke the hosted agent through the central model (project -> APIM -> provider A).

Hosted agents are NOT called like prompt agents (no `agent_reference`). They expose
their own agent endpoint speaking the OpenAI Responses protocol:

    {project_endpoint}/agents/{agentName}/endpoint/protocols/openai/responses?api-version=v1

Usage:  python invoke.py "<CONSUMER_PROJECT_ENDPOINT>" ["message"]
        (or set AZURE_AI_PROJECT_ENDPOINT)
"""

import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

endpoint = sys.argv[1] if len(sys.argv) > 1 else os.environ["AZURE_AI_PROJECT_ENDPOINT"]
message = sys.argv[2] if len(sys.argv) > 2 else "Say hi via the gateway."
agent = os.environ.get("HOSTED_AGENT_NAME", "gateway-hosted")

proj = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True)
client = proj.get_openai_client().with_options(
    base_url=f"{endpoint}/agents/{agent}/endpoint/protocols/openai",
    default_query={"api-version": "v1"},  # agent endpoint versions via query, not /openai/v1/ path
)

# A freshly deployed container is cold: the first session can take a minute to pull the
# image and boot, returning 424 session_not_ready. Retry a few times before giving up.
for attempt in range(6):
    try:
        print(client.responses.create(model=agent, input=message).output_text)
        break
    except Exception as e:
        if "session_not_ready" in str(e) and attempt < 5:
            time.sleep(30)
            continue
        raise
