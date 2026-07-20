"""Tiny hosted (containerized) Foundry agent.

Runs the same image in each consumer project. It answers using the central model
served through the shared APIM gateway: the model deployment name is the BYOM
connection route "<connection>/<model>" (e.g. apim-shared/gpt-4.1), so every call
goes consumer project -> APIM -> provider Foundry, keyless.

Env (injected by the deploy hook via the hosted-agent definition):
  AZURE_AI_PROJECT_ENDPOINT       this project's Foundry endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME  the gateway model route, e.g. apim-shared/gpt-4.1
"""

import os

from agent_framework_foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential

agent = FoundryChatClient(
    project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=DefaultAzureCredential(),  # the hosted agent's own injected identity
).as_agent(
    name="gateway-hosted",
    instructions=(
        "You are a helpful assistant. You answer using a model served through the "
        "shared Azure API Management gateway."
    ),
)

if __name__ == "__main__":
    ResponsesHostServer(agent).run()  # serves the Responses protocol on :8088
