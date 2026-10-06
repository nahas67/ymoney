"""Does `response_model` with `extra="allow"` preserve fields the model omits?

Work 16.5.4 §2 prefers `response_model=` over `responses={}`, but `response_model`
VALIDATES AND FILTERS: a model missing one key silently drops that key from the
wire while every test stays green. That is the single biggest risk in generating
172 contracts from observed runtime data, because an observation cannot prove a
field is absent from every unobserved state.

So this is measured, not assumed. A throwaway app serves two payloads that differ
by exactly one field, and both are fetched back under three configurations:

    response_model            plain, no extra="allow"
    response_model + extra    model_config = ConfigDict(extra="allow")
    responses=                documentation only

If `extra="allow"` did not preserve the unknown field, generating 172 contracts
with it would be exactly the fiction §1 forbids, and the whole approach would
have to change.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

app = FastAPI()


class Envelope(BaseModel):
    known: str


class EnvelopeExtra(BaseModel):
    model_config = ConfigDict(extra="allow")

    known: str


@app.get("/plain")
def plain() -> Envelope:
    # The handler returns MORE than the model declares.
    return {"known": "a", "undeclared_but_real": "must survive"}


@app.get("/extra")
def extra() -> EnvelopeExtra:
    return {"known": "a", "undeclared_but_real": "must survive"}


@app.get("/documented", responses={200: {"model": Envelope}})
def documented() -> dict:
    return {"known": "a", "undeclared_but_real": "must survive"}


client = TestClient(app)

EXPECTED = "must survive"
results = {}

r = client.get("/plain")
results["response_model (no extra=allow)"] = (r.status_code, r.json())

r = client.get("/extra")
results["response_model + extra=allow"] = (r.status_code, r.json())

r = client.get("/documented")
results["responses={} (documentation only)"] = (r.status_code, r.json())

print("=" * 78)
print("PROBE: does an undeclared field survive the response pipeline?")
print("=" * 78)

survivors = {}
for label, (status, body) in results.items():
    survived = body.get("undeclared_but_real") == EXPECTED
    survivors[label] = survived
    print(f"\n  {label}")
    print(f"    status  {status}")
    print(f"    body    {body}")
    print(f"    field survived: {survived}")

print()
print("=" * 78)
print("VERDICT")
print("=" * 78)
print(f"  response_model WITHOUT extra=allow preserves undeclared fields: "
      f"{survivors['response_model (no extra=allow)']}")
print(f"  response_model WITH    extra=allow preserves undeclared fields: "
      f"{survivors['response_model + extra=allow']}")
print(f"  responses={{}} documentation-only preserves undeclared fields:   "
      f"{survivors['responses={} (documentation only)']}")
print()
if survivors["response_model + extra=allow"]:
    print("  => SAFE to use response_model with extra='allow' for generated")
    print("     contracts: a field missed during observation is PRESERVED")
    print("     rather than silently dropped.")
else:
    print("  => UNSAFE. Generated contracts must use responses={} instead,")
    print("     because response_model would silently drop real data.")